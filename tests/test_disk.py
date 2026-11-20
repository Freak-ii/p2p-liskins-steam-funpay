import shutil
from collections import namedtuple

from center.app import Center

GB = 1 << 30
Usage = namedtuple("Usage", "total used free")


def test_low_disk_warns_once_a_day(tmp_path, monkeypatch):
    c = Center(tmp_path)
    free = {"v": 50 * GB}
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(100 * GB, 100 * GB - free["v"], free["v"]))
    d = c.monitor.disk()
    assert not d["low"] and d["disks"][0]["label"] == "данные центра" and d["disks"][0]["free_gb"] == 50.0
    assert len(d["disks"]) == 1                                  # копии на том же диске — одна строка
    assert c.monitor.check_disk() is None

    free["v"] = int(1.5 * GB)
    msg = c.monitor.check_disk()
    assert msg and "свободно 1.5 ГБ" in msg
    ev = c.events.recent(5)[0]
    assert ev["kind"] == "monitor.low_disk" and ev["level"] == "warn"
    assert c.monitor.check_disk() is None                        # второй раз за сутки молчит
    assert "Мало места" in c.monitor.daily_report()
    assert c.monitor.status()["disk"]["low"]

    c.monitor.kv_set("low_disk_date", "")
    free["v"] = int(0.3 * GB)
    c.monitor.check_disk()
    assert c.events.recent(5)[0]["level"] == "critical"

    c.cfg.save({"monitor": {"disk": {"warn_gb": 0}}})            # 0 — не следить
    assert not c.monitor.disk()["low"]
    c.events.close()


def test_separate_backup_disk(tmp_path, monkeypatch):
    c = Center(tmp_path)
    other = tmp_path / "cloud"
    other.mkdir()
    c.cfg.save({"backup": {"dir": str(other / "copies")}})
    real_stat = type(other).stat

    def fake_stat(self, *a, **k):
        st = real_stat(self, *a, **k)
        if str(self).startswith(str(other)):
            class S:
                st_dev = st.st_dev + 1
            return S()
        return st
    monkeypatch.setattr(type(other), "stat", fake_stat)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: Usage(10 * GB, 9 * GB, GB) if str(p).startswith(str(other))
                        else Usage(100 * GB, 10 * GB, 90 * GB))
    d = c.monitor.disk()
    assert [x["label"] for x in d["disks"]] == ["данные центра", "резервные копии"]
    assert d["disks"][1]["low"] and not d["disks"][0]["low"]
    c.events.close()
