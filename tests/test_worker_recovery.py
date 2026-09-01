"""Reconexión del Worker y ventana de historial.

Los dos fallos que dejaban a una cuenta "conectada" sin datos:
  - el password se borraba tras el primer login → ningún re-login funcionaba,
  - la ventana de historial se cortaba en UTC → los cierres recientes de un
    bróker en hora adelantada nunca se emitían.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.worker import mt5_client as mt5_module
from app.worker.mt5_client import MT5Client
from app.worker.worker import Worker


class _FakeEmitter:
    url = "http://core.test/events"

    def __init__(self) -> None:
        self.events: list[str] = []

    def emit(self, event) -> bool:  # noqa: ANN001
        self.events.append(event.event)
        return True

    def stats(self) -> dict:
        return {"sent": len(self.events)}

    def close(self) -> None:
        pass


class _FakeClient:
    def __init__(self, fail_first: bool = False) -> None:
        self.passwords: list[str] = []
        self.fail_first = fail_first

    def initialize_and_login(self, login: int, password: str, server: str) -> None:
        self.passwords.append(password)
        if self.fail_first and len(self.passwords) == 1:
            raise RuntimeError("IPC timeout")

    def shutdown(self) -> None:
        pass


def _worker(tmp_path: Path, client: _FakeClient) -> Worker:
    worker = Worker(
        slot_id="Slot-01",
        account_id="acc-1",
        mt5_login=999,
        mt5_password="investor-secreta",
        mt5_server="Broker-Server",
        terminal_path=tmp_path,
        core_events_url="http://core.test/events",
        core_api_key="k" * 8,
        login_retry_max=2,
        login_retry_backoff_sec=0.0,
    )
    worker.client = client  # type: ignore[assignment]
    worker.emitter = _FakeEmitter()  # type: ignore[assignment]
    return worker


def test_reconexion_reutiliza_el_password(tmp_path: Path) -> None:
    """El segundo login (reconexión tras caída de MT5) debe llevar el password."""
    client = _FakeClient()
    worker = _worker(tmp_path, client)

    assert worker._login_with_retries() is True
    assert worker._login_with_retries() is True

    assert client.passwords == ["investor-secreta", "investor-secreta"]


def test_reintento_de_login_conserva_el_password(tmp_path: Path) -> None:
    client = _FakeClient(fail_first=True)
    worker = _worker(tmp_path, client)

    assert worker._login_with_retries() is True
    assert client.passwords == ["investor-secreta", "investor-secreta"]


def test_runtime_json_publica_el_estado_del_canal(tmp_path: Path) -> None:
    import json

    worker = _worker(tmp_path, _FakeClient())
    worker._write_runtime("connected")

    payload = json.loads((tmp_path / "slot_runtime.json").read_text(encoding="utf-8"))
    assert payload["status"] == "connected"
    assert payload["core_events_url"] == "http://core.test/events"
    assert payload["emit"] == {"sent": 0}


def test_ventana_de_historial_cubre_la_hora_del_broker(monkeypatch, tmp_path: Path) -> None:
    """El tope va por delante de UTC: el bróker marca los deals en su propia hora."""
    capturado: dict[str, datetime] = {}

    class _FakeMT5:
        @staticmethod
        def history_deals_get(date_from: datetime, date_to: datetime):
            capturado["from"] = date_from
            capturado["to"] = date_to
            return ()

    monkeypatch.setattr(mt5_module, "mt5", _FakeMT5)
    client = MT5Client(tmp_path, history_forward_buffer_hours=24.0)
    client._connected = True

    assert client.history_deals(lookback_days=7) == ()

    ahora = datetime.now(timezone.utc)
    # Un bróker en EET (UTC+3) marca "ahora" hasta 3h por delante de UTC.
    assert capturado["to"] > ahora + timedelta(hours=3)
    assert capturado["from"] < ahora - timedelta(days=7)


def test_buffer_cero_reproduce_el_fallo_original(monkeypatch, tmp_path: Path) -> None:
    capturado: dict[str, datetime] = {}

    class _FakeMT5:
        @staticmethod
        def history_deals_get(date_from: datetime, date_to: datetime):
            capturado["to"] = date_to
            return ()

    monkeypatch.setattr(mt5_module, "mt5", _FakeMT5)
    client = MT5Client(tmp_path, history_forward_buffer_hours=0.0)
    client._connected = True
    client.history_deals(lookback_days=7)

    ahora = datetime.now(timezone.utc)
    assert capturado["to"] <= ahora + timedelta(seconds=1)


def test_error_del_canal_al_core_se_marca_y_se_limpia() -> None:
    """El Manager refleja el fallo de entrega y lo retira al recuperarse."""
    from app.manager.slot_manager import _core_channel_error

    fallando = {
        "last_error": "HTTP 401: unauthorized",
        "last_error_at": "2026-08-23T10:00:05Z",
        "last_ok_at": None,
    }
    marcado = _core_channel_error(fallando, None)
    assert marcado is not None and "401" in marcado

    recuperado = {
        "last_error": "HTTP 401: unauthorized",
        "last_error_at": "2026-08-23T10:00:05Z",
        "last_ok_at": "2026-08-23T10:00:30Z",
    }
    assert _core_channel_error(recuperado, marcado) is None

    # Un error ajeno al canal (p.ej. login MT5) no se pisa.
    assert _core_channel_error(recuperado, "mt5.login falló") == "mt5.login falló"


def test_trade_rechazado_por_el_core_se_reintenta(tmp_path: Path) -> None:
    """Un trade.closed que el Core no acepta no puede perderse.

    pop_ready_trades marca como emitido ANTES de enviar; sin deshacer esa
    marca, un rechazo del Core borraba la operación del historial para
    siempre. Reproduce el caso real: el Core respondía 200 y fallaba al
    guardar (user_id sin valor por defecto).
    """

    class _EmitterQueRechaza(_FakeEmitter):
        def emit(self, event) -> bool:  # noqa: ANN001
            self.events.append(event.event)
            return event.event != "trade.closed"

    worker = _worker(tmp_path, _FakeClient())
    worker.emitter = _EmitterQueRechaza()  # type: ignore[assignment]

    class _Trade:
        position_id = 608658310
        symbol = "ETHUSDm"
        direction = "sell"
        volume = 0.1
        profit = -0.03

        def to_event_data(self):
            from app.models.events import ClosedTradeData

            return ClosedTradeData(
                positionId="608658310",
                dealId="1",
                symbol="ETHUSDm",
                type="sell",
                volume=0.1,
                openPrice=1.0,
                closePrice=1.0,
                openTime="2026-08-24T00:00:00Z",
                closeTime="2026-08-24T00:10:00Z",
                profit=-0.03,
            )

    worker.matcher.mark_emitted([608658310])
    worker._emit_trade(_Trade())

    # Desmarcado y en cola: el siguiente poll de historial lo reintenta.
    assert 608658310 not in worker.matcher.emitted_position_ids
    assert 608658310 in worker._pending_closed


def test_trade_aceptado_queda_marcado(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _FakeClient())
    worker.matcher.mark_emitted([777])
    assert 777 in worker.matcher.emitted_position_ids
    worker.matcher.unmark_emitted([777])
    assert 777 not in worker.matcher.emitted_position_ids


# --- Libro de posiciones: lo que el Core no acepta se reintenta -------------

def _pos(ticket: int, sl: float = 0.0):
    from datetime import datetime, timezone

    from app.worker.mt5_client import PositionSnapshot

    return PositionSnapshot(
        ticket=ticket,
        symbol="GBPUSD",
        type="sell",
        volume=0.23,
        open_price=1.36437,
        current_price=1.36404,
        sl=sl,
        tp=0.0,
        profit=7.59,
        swap=0.0,
        commission=0.0,
        open_time=datetime(2026, 8, 24, 13, 30, tzinfo=timezone.utc),
        comment="",
        magic=0,
    )


class _ClienteConPosiciones(_FakeClient):
    def __init__(self, posiciones) -> None:
        super().__init__()
        self._posiciones = posiciones

    def positions(self):
        return self._posiciones


def _worker_con(tmp_path: Path, posiciones, acepta: bool):
    class _Emitter(_FakeEmitter):
        def emit(self, event) -> bool:  # noqa: ANN001
            self.events.append(event.event)
            return acepta or not event.event.startswith("position.")

    worker = _worker(tmp_path, _ClienteConPosiciones(posiciones))
    worker.emitter = _Emitter()  # type: ignore[assignment]
    return worker


def test_position_opened_rechazada_se_reintenta(tmp_path: Path) -> None:
    """El caso real: el Core devuelve 200 y falla al guardar en
    posiciones_abiertas. Sin reintento, la posición desaparecía del libro del
    Core hasta el siguiente arranque del Worker."""
    worker = _worker_con(tmp_path, [_pos(9457323)], acepta=False)

    worker._poll_positions()
    assert 9457323 not in worker._positions  # no se da por conocida

    worker._poll_positions()
    assert worker.emitter.events.count("position.opened") == 2  # reemitida


def test_position_opened_aceptada_no_se_repite(tmp_path: Path) -> None:
    worker = _worker_con(tmp_path, [_pos(9457323)], acepta=True)

    worker._poll_positions()
    worker._poll_positions()

    assert worker.emitter.events.count("position.opened") == 1
    assert 9457323 in worker._positions


def test_position_updated_rechazada_conserva_el_estado_anterior(tmp_path: Path) -> None:
    """Si el cambio no entra, el poll siguiente lo vuelve a detectar."""
    posiciones = [_pos(9457323)]
    worker = _worker_con(tmp_path, posiciones, acepta=True)
    worker._poll_positions()

    # Ahora el Core deja de aceptar y la posición cambia de stop loss.
    worker.emitter.emit = lambda event: (  # type: ignore[method-assign]
        worker.emitter.events.append(event.event) or False
    ) if event.event.startswith("position.") else True
    posiciones[0] = _pos(9457323, sl=1.36425)

    worker._poll_positions()
    assert worker._positions[9457323].sl == 0.0  # se conserva el anterior

    worker._poll_positions()
    assert worker.emitter.events.count("position.updated") == 2


def test_position_closed_rechazada_se_reintenta(tmp_path: Path) -> None:
    posiciones = [_pos(9457323)]
    worker = _worker_con(tmp_path, posiciones, acepta=True)
    worker._poll_positions()

    worker.emitter.emit = lambda event: (  # type: ignore[method-assign]
        worker.emitter.events.append(event.event) or False
    ) if event.event.startswith("position.") else True
    posiciones.clear()  # la posición se cierra en MT5

    worker._poll_positions()
    assert 9457323 in worker._positions  # sigue "abierta" para nosotros

    worker._poll_positions()
    assert worker.emitter.events.count("position.closed") == 2


def test_sin_libro_de_abiertas_no_se_emiten_posiciones(tmp_path: Path) -> None:
    """El Core que solo quiere operaciones cerradas no debe recibir el libro.

    Sin este interruptor, un Core que rechaza position.opened haría que el
    Worker lo reintentara en cada poll, para siempre.
    """
    worker = _worker_con(tmp_path, [_pos(9457323)], acepta=True)
    worker.emit_position_events = False

    worker._poll_positions()
    worker._poll_positions()

    assert worker.emitter.events == []          # nada emitido
    assert 9457323 in worker._positions          # pero sí se sigue por dentro


def test_sin_libro_de_abiertas_los_cierres_se_siguen_detectando(tmp_path: Path) -> None:
    """El seguimiento interno alimenta el emparejado de trade.closed."""
    posiciones = [_pos(9457323)]
    worker = _worker_con(tmp_path, posiciones, acepta=True)
    worker.emit_position_events = False

    worker._poll_positions()
    posiciones.clear()
    worker._poll_positions()

    assert worker.emitter.events == []
    assert 9457323 in worker._pending_closed     # listo para emparejar el cierre
    assert worker._sl_tp.get(9457323) is not None


# --- Replay del historial al conectar ---------------------------------------
#
# El fallo real: al reconectar una cuenta, los trades viejos llegaban al Core
# con el stop en null. Las órdenes se leían en el poll periódico pero NO en el
# replay, que es justo donde más falta hacen: esos trades cerraron antes de
# que existiera el Worker, nadie sondeó su posición, y el `sl` del deal viene
# en 0 con la mayoría de brókers.


class _ClienteConHistorial(_FakeClient):
    """Un bróker típico: el stop está en la orden, el deal lo trae en 0."""

    SL_DE_LA_ORDEN = 1.07800

    def history_deals(self, lookback_days: int):  # noqa: ANN201
        abre = _Deal(ticket=11, entry=0, deal_type=0, order=500, price=1.08000)
        cierra = _Deal(ticket=22, entry=1, deal_type=1, order=501, price=1.08400)
        return [abre, cierra]

    def history_orders(self, lookback_days: int):  # noqa: ANN201
        return [_Orden(ticket=500, sl=self.SL_DE_LA_ORDEN, tp=1.08600)]

    def positions(self):  # noqa: ANN201
        return []

    def account_info(self):  # noqa: ANN201
        from app.worker.mt5_client import AccountSnapshot

        return AccountSnapshot(
            login=999,
            balance=10_000.0,
            equity=10_000.0,
            margin=0.0,
            free_margin=10_000.0,
            margin_level=None,
            profit=0.0,
            currency="USD",
            leverage=100,
            name="Prueba",
            server="Broker-Server",
        )


class _Deal:
    def __init__(self, *, ticket, entry, deal_type, order, price):  # noqa: ANN001
        self.ticket = ticket
        self.position_id = 987654321
        self.symbol = "EURUSD"
        self.type = deal_type
        self.entry = entry
        self.volume = 0.10
        self.price = price
        self.profit = 4.0
        self.swap = 0.0
        self.commission = 0.0
        self.time = int(datetime(2026, 8, 21, 14, 0, tzinfo=timezone.utc).timestamp())
        self.order = order
        self.sl = 0.0  # el bróker no lo rellena aquí
        self.tp = 0.0
        self.magic = 0
        self.comment = ""


class _Orden:
    def __init__(self, *, ticket, sl, tp):  # noqa: ANN001
        self.ticket = ticket
        self.position_id = 987654321
        self.sl = sl
        self.tp = tp


def _worker_con_replay(tmp_path: Path) -> Worker:
    worker = _worker(tmp_path, _ClienteConHistorial())
    worker.replay_history_on_connect = True
    return worker


def test_el_replay_recupera_el_stop_desde_la_orden(tmp_path: Path) -> None:
    worker = _worker_con_replay(tmp_path)
    emitidos: list[dict] = []
    worker._emit = lambda event, data: (emitidos.append({event: data}) or True)  # type: ignore[assignment,method-assign]

    worker._bootstrap_history()

    cerrados = [list(e.values())[0] for e in emitidos if "trade.closed" in e]
    assert len(cerrados) == 1
    trade = cerrados[0]
    assert trade["initialStopLoss"] == _ClienteConHistorial.SL_DE_LA_ORDEN
    # Y sin nada más reciente, también es el último conocido.
    assert trade["stopLoss"] == _ClienteConHistorial.SL_DE_LA_ORDEN


def test_un_fallo_leyendo_las_ordenes_no_impide_el_replay(tmp_path: Path) -> None:
    """Quedarse sin stop es un incordio; perder la operación, no."""

    class _SinOrdenes(_ClienteConHistorial):
        def history_orders(self, lookback_days: int):  # noqa: ANN201
            raise RuntimeError("history_orders_get reventó")

    worker = _worker(tmp_path, _SinOrdenes())
    worker.replay_history_on_connect = True
    emitidos: list[dict] = []
    worker._emit = lambda event, data: (emitidos.append({event: data}) or True)  # type: ignore[assignment,method-assign]

    worker._bootstrap_history()

    cerrados = [list(e.values())[0] for e in emitidos if "trade.closed" in e]
    assert len(cerrados) == 1
    assert cerrados[0]["initialStopLoss"] is None


# --- Estado local: de quién es -----------------------------------------------
#
# worker_state.json guarda los position_id ya emitidos y vive en la carpeta del
# SLOT, que se reutiliza entre cuentas. Sin comprobar el dueño, la cuenta que
# entra hereda los ids de la anterior y da por enviados trades que nunca mandó.
# Los position_id los numera cada servidor de bróker, así que chocan.


def _con_estado(tmp_path: Path, payload: dict) -> Worker:
    import json as _json

    (tmp_path / "worker_state.json").write_text(
        _json.dumps(payload), encoding="utf-8"
    )
    return _worker(tmp_path, _FakeClient())


def test_el_estado_de_otra_cuenta_se_ignora(tmp_path: Path) -> None:
    """Heredarlo silenciaría trades reales del cliente que entra."""
    worker = _con_estado(
        tmp_path,
        {"account_id": "otra-cuenta", "mt5_login": 111, "matcher": {"emitted": [42]}},
    )

    worker._load_persist()

    assert worker.matcher.dump_persist().get("emitted", []) == []


def test_el_estado_propio_se_carga(tmp_path: Path) -> None:
    worker = _worker(tmp_path, _FakeClient())
    worker.matcher.mark_emitted([42])
    worker._save_persist()

    otro = _worker(tmp_path, _FakeClient())
    otro._load_persist()

    assert 42 in otro.matcher.dump_persist().get("emitted", [])


def test_un_estado_sin_dueno_se_ignora(tmp_path: Path) -> None:
    """Formato viejo: no se puede saber de quién es, así que no vale.

    Repetir un trade lo arregla el Core con su clave única; perderlo no lo
    arregla nadie.
    """
    worker = _con_estado(tmp_path, {"emitted": [42]})

    worker._load_persist()

    assert worker.matcher.dump_persist().get("emitted", []) == []


def test_el_estado_guardado_dice_de_quien_es(tmp_path: Path) -> None:
    import json as _json

    worker = _worker(tmp_path, _FakeClient())
    worker._save_persist()

    guardado = _json.loads((tmp_path / "worker_state.json").read_text(encoding="utf-8"))
    assert guardado["account_id"] == "acc-1"
    assert guardado["mt5_login"] == 999
