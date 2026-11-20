from center.app import Center
from center.monitor import error_signature


def test_error_signature_groups_same_errors():
    a = error_signature("2026-10-18T10:00:01.123Z ERROR [lis] order 12345 failed after 3.5s")
    b = error_signature("2026-10-18T11:20:41.000Z ERROR [lis] order 777 failed after 10.0s")
    assert a == b == "ERROR [lis] order # failed after #.#s"


def test_error_spike_notifies_once(tmp_path):
    c = Center(tmp_path)
    now = {"t": 1_800_000_000.0}
    c.monitor.clock = lambda: now["t"]
    w = c.workers["lisbot"]

    def out(text, ago=60):
        w.seq += 1
        w.lines.append({"n": w.seq, "ts": now["t"] - ago, "stream": "stdout", "text": text})

    for i in range(19):
        out(f"2026-10-18T10:00:00.000Z ERROR [steam] trade {i} rate limited")
    out("2026-10-18T10:00:00.000Z INFO  [lis] всё хорошо")
    out("2026-10-18T08:00:00.000Z ERROR [lis] старая ошибка", ago=7200)       # больше часа назад
    assert c.monitor.check_errors() == []
    assert c.monitor.status()["errors"]["lisbot"] == 19
    out("2026-10-18T10:00:00.000Z ERROR [lis] нет сети")
    msg = c.monitor.check_errors()
    assert len(msg) == 1 and "20 ошибок" in msg[0] and "(19 раз)" in msg[0] and "rate limited" in msg[0]
    ev = c.events.recent(5)[0]
    assert ev["kind"] == "monitor.error_spike" and ev["source"] == "lisbot"
    assert c.monitor.check_errors() == []                     # не чаще раза в 6 часов
    now["t"] += 6 * 3600 + 1
    for i in range(20):
        out("Traceback (most recent call last):")
    assert len(c.monitor.check_errors()) == 1
    c.cfg.save({"monitor": {"errors": {"per_hour": 0}}})
    now["t"] += 6 * 3600 + 1
    for i in range(30):
        out("FATAL x")
    assert c.monitor.check_errors() == []
