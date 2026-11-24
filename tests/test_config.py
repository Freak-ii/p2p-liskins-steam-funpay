import time

import yaml

from center.app import Center
from center.backup import make_backup
from center.config import CenterConfig, check_config


def test_typos_and_wrong_types_are_reported(tmp_path):
    (tmp_path / "center.yaml").write_text(yaml.safe_dump({
        "monitor": {"dedup_minuts": 5, "disk": {"warn_gb": "два"}, "telegram": {"commands": False}},
        "backup": {"enabled": "да"}, "budget": {"plan": {"lisbot.limits.x": 1}},
        "workers": {"lisbot": {"port": 9000}}, "coordinator": {"permissions": {"pause_bot": "auto"}},
        "analytics": {"usd_rub": 95}}, allow_unicode=True), encoding="utf-8")
    c = Center(tmp_path)
    probs = [t for _, t in c.cfg.problems]
    assert len(probs) == 3
    assert any("monitor.dedup_minuts" in p for p in probs) and any("warn_gb должно быть числом" in p for p in probs)
    assert any("backup.enabled должно быть true или false" in p for p in probs)
    c.events.close()


def test_settings_written_by_panel_pass_the_check(tmp_path):
    c = Center(tmp_path)
    c.monitor.save_settings({"telegram_chat_id": "1", "telegram_commands": False, "quiet_hours": "23:00-08:00",
                             "heartbeat_url": "https://hc", "daily_report_time": "09:00"})
    c.backup.save_settings({"enabled": True, "time": "03:00", "keep": 5, "dir": ""})
    c.analytics.save_goal(1000)
    c.cfg.ensure_token()
    assert check_config(c.cfg.user) == []
    c.events.close()


def test_broken_yaml_restored_from_backup(tmp_path):
    good = {"center": {"port": 8900}, "analytics": {"month_goal_rub": 777}}
    (tmp_path / "center.yaml").write_text(yaml.safe_dump(good), encoding="utf-8")
    make_backup(tmp_path, tmp_path / "backups", now=time.time() - 60)
    (tmp_path / "center.yaml").write_text("center:\n  port: 8900\n  bad: [1, 2\n", encoding="utf-8")   # незакрытая скобка
    cfg = CenterConfig(tmp_path)
    assert cfg.data["center"]["port"] == 8900 and cfg.data["analytics"]["month_goal_rub"] == 777
    level, text = cfg.problems[0]
    assert level == "critical" and "строка" in text and "из резервной копии" in text
    assert list(tmp_path.glob("center.yaml.broken-*")) and yaml.safe_load((tmp_path / "center.yaml").read_text(encoding="utf-8")) == good


def test_broken_yaml_without_backup_uses_defaults_and_logs(tmp_path):
    (tmp_path / "center.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    c = Center(tmp_path)
    assert c.cfg.data["center"]["port"] == 8800 and "по умолчанию" in c.cfg.problems[0][1]
    ev = [e for e in c.events.recent(20) if e["kind"] == "center.config"]
    assert ev and ev[0]["level"] == "critical"
    c.events.close()


def test_demo_defaults_do_not_clash_with_live_ports(tmp_path):
    """Демо запускается рядом с боевым центром: свои порты и автозапуск всех ботов."""
    live, demo = CenterConfig(tmp_path / "live"), CenterConfig(tmp_path / "demo", demo=True)
    assert live["center"]["port"] == 8800 and not live.worker("mannbot")["autostart"]
    ports = {demo["center"]["port"]} | {demo.worker(n)["port"] for n in ("mannbot", "lisbot", "starsbot")}
    assert ports == {18800, 18801, 18802, 18803}
    assert all(demo.autostart(n) for n in ("mannbot", "lisbot", "starsbot"))
    assert not any(live.autostart(n) for n in ("mannbot", "lisbot", "starsbot", "shopbot", "tggift"))
    demo.save({"workers": {"lisbot": {"autostart": False}}})
    assert not demo.autostart("lisbot") and demo.worker("mannbot")["port"] == 18801
