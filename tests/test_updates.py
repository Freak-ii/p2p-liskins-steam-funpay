import asyncio

from center import __version__
from center.app import Center
from center.updates import version_tuple


def test_version_tuple():
    assert version_tuple("center-v0.10.0") > version_tuple("0.9.12") > version_tuple("0.9.1")


def test_new_version_noticed_once(tmp_path):
    async def go():
        c = Center(tmp_path)
        cur = version_tuple(__version__)
        newer = f"{cur[0]}.{cur[1]}.{cur[2] + 1}"
        rel = [{"tag_name": f"center-v{newer}", "html_url": "https://example/rel", "draft": False},
               {"tag_name": "center-v0.1.0", "html_url": "x", "draft": False},
               {"tag_name": "center-v99.0.0", "html_url": "y", "draft": True},          # черновик не считается
               {"tag_name": "lisbot-v9.9.9", "html_url": "z", "draft": False}]

        async def fake():
            return rel
        c.updates._fetch = fake
        st = await c.updates.check()
        assert st["newer"] and st["latest"] == newer and st["url"] == "https://example/rel"
        ev = [e for e in c.events.recent(10) if e["kind"] == "center.update"]
        assert len(ev) == 1 and newer in ev[0]["message"]
        await c.updates.check()                                             # о той же версии — один раз
        assert len([e for e in c.events.recent(10) if e["kind"] == "center.update"]) == 1

        async def same():
            return [{"tag_name": f"center-v{__version__}", "html_url": "u", "draft": False}]
        c.updates._fetch = same
        assert not (await c.updates.check())["newer"]

        async def offline():
            raise OSError("нет сети")
        c.updates._fetch = offline
        assert (await c.updates.check())["error"] == "нет сети"
        c.events.close()
    asyncio.run(go())


def test_disabled_in_demo_and_config(tmp_path):
    c = Center(tmp_path, demo=True)
    assert not c.updates.enabled
    c.events.close()
    c2 = Center(tmp_path / "b")
    c2.cfg.save({"center": {"update_check": False}})
    assert not c2.updates.enabled
    c2.events.close()
