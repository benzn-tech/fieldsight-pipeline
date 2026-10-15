"""The pypdf layer is built at the version the tests run against, under the name and
runtime the template expects (R13). The script is run by hand once per account, so
nothing else would notice it drifting."""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "build-pypdf-layer.sh")


def _text(p):
    return open(p, encoding="utf-8").read()


def test_the_layer_version_is_the_tested_version():
    pinned = re.search(r"^pypdf==([\d.]+)$", _text(os.path.join(REPO, "requirements-test.txt")),
                       re.M).group(1)
    assert f'PYPDF_VERSION="{pinned}"' in _text(SCRIPT)


def test_the_layer_name_runtime_and_layout():
    s = _text(SCRIPT)
    assert '--layer-name fieldsight-pypdf' in s
    assert '--compatible-runtimes python3.12' in s
    assert 'python/' in s                       # Lambda imports layers from /opt/python
    assert '--no-deps' in s                     # pure Python, nothing else in the layer


def test_publishing_is_opt_in():
    s = _text(SCRIPT)
    assert '"${2:-}" = "--publish"' in s or "'--publish'" in s or '"--publish"' in s
    assert "publish-layer-version" in s
