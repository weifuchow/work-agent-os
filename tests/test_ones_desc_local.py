from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ONES_CLI = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "ones" / "scripts" / "ones_cli.py"


def _load_ones_cli():
    module_name = "ones_cli_desc_local_test"
    if str(ONES_CLI.parent) not in sys.path:
        sys.path.insert(0, str(ONES_CLI.parent))
    spec = importlib.util.spec_from_file_location(module_name, ONES_CLI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_extract_desc_image_refs_handles_attribute_order_and_mime():
    module = _load_ones_cli()
    desc_rich = (
        '<p>问题现象</p>'
        '<img data-mime="image/png" data-ref-id="XpVaojiZ" data-uuid="7yKRFpP8" src="https://example.com/1" />'
        '<img data-uuid="LCgAzruB" src="data:image/jpeg;base64,AAAA" data-mime="image/jpeg" />'
    )
    refs = module.extract_desc_image_refs(desc_rich)

    assert len(refs) == 2
    assert refs[0]["uuid"] == "7yKRFpP8"
    assert refs[0]["mime"] == "image/png"
    assert refs[1]["uuid"] == "LCgAzruB"
    assert refs[1]["mime"] == "image/jpeg"


def test_build_desc_local_prefers_text_then_embeds_local_image_markers():
    module = _load_ones_cli()
    desc = "1.问题出现时间：2026年4月10号上午11:30左右\n2.问题现象：见图"
    desc_rich = (
        "<p>1.问题出现时间：2026年4月10号上午11:30左右</p>"
        "<p>2.问题现象：见图</p>"
        '<figure><img data-mime="image/png" data-uuid="7yKRFpP8" src="https://example.com/1" /></figure>'
    )
    desc_files = [
        {
            "label": "description_image_01_7yKRFpP8",
            "path": r"D:\tmp\description_image_01_7yKRFpP8.png",
            "uuid": "7yKRFpP8",
            "mime": "image/png",
        }
    ]

    rendered = module.build_desc_local(desc, desc_rich, desc_files)

    assert "问题出现时间：2026年4月10号上午11:30左右" in rendered
    assert "问题现象：见图" in rendered
    assert 'path="D:\\tmp\\description_image_01_7yKRFpP8.png"' in rendered
    assert 'mime="image/png"' in rendered
