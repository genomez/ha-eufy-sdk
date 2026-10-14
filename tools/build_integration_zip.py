"""Build the pinned integration-only manual artifact with standard-library tools."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PREFIX = 'custom_components/eufy_sdk/'
DOCUMENTS = {'README.md', 'INSTALL.md', 'CORE_COMPATIBILITY.md', 'EXPERIMENTAL.md',
             'LICENSE', 'INTEGRATION_NOTICES.md'}


def build(output_dir):
    """Verify the allowlist and write deterministic ZIP/hash/content receipts."""
    metadata = json.loads((ROOT / 'INTEGRATION_PACKAGE.json').read_text(encoding='utf-8'))
    assert metadata['version'] == '0.3.1.dev1'
    assert metadata['domain'] == 'eufy_sdk'
    assert metadata['owner'] == 'genomez'
    assert metadata['filename'] == 'eufy-sdk-s1-0.3.1.dev1.zip'
    expected = {item['path']: item['sha256'] for item in metadata['files']}
    assert len(expected) == len(metadata['files']), 'Duplicate allowlist member'
    for rel in expected:
        parts = Path(rel).parts
        assert '..' not in parts and not Path(rel).is_absolute()
        assert rel.startswith(PREFIX) or rel in DOCUMENTS, 'Unapproved package path'
    actual = set()
    for path in (ROOT / 'custom_components/eufy_sdk').rglob('*'):
        assert not path.is_symlink(), 'Symlinks are not packaged'
        if path.is_file():
            actual.add(path.relative_to(ROOT).as_posix())
    assert actual == {rel for rel in expected if rel.startswith(PREFIX)}, 'Runtime file set changed'
    assert {rel for rel in expected if not rel.startswith(PREFIX)} == DOCUMENTS
    inputs = {}
    for rel, digest in sorted(expected.items()):
        path = ROOT / rel
        assert path.resolve().is_relative_to(ROOT)
        assert not path.is_symlink()
        data = path.read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, f'Input changed: {rel}'
        inputs[rel] = data
    manifest = json.loads(inputs[PREFIX + 'manifest.json'])
    assert manifest['domain'] == metadata['domain'] and manifest['version'] == metadata['version']
    assert manifest['codeowners'] == ['@genomez']
    destination = Path(output_dir).resolve()
    assert not destination.is_relative_to(ROOT), 'Artifacts must be outside the source tree'
    destination.mkdir(parents=True, exist_ok=True)
    artifact = destination / metadata['filename']
    assert not artifact.exists(), 'Retain previous artifact; choose a new output directory'
    with zipfile.ZipFile(artifact, 'x', compression=zipfile.ZIP_STORED) as archive:
        for rel, data in inputs.items():
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, data)
    with zipfile.ZipFile(artifact) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == sorted(expected)
        assert {rel: hashlib.sha256(archive.read(rel)).hexdigest() for rel in archive.namelist()} == expected
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (destination / (artifact.name + '.sha256')).write_text(digest + '  ' + artifact.name + '\n', encoding='utf-8')
    receipt = {'filename': artifact.name, 'version': metadata['version'], 'sha256': digest,
               'sizeBytes': artifact.stat().st_size, 'files': metadata['files'],
               'zipTimestamp': '1980-01-01T00:00:00', 'compression': 'stored',
               'permissions': 'regular file 0644', 'runtimeDomainCount': 1,
               'frontendIncluded': False, 'coreOverlaysIncluded': False,
               'gitMetadataIncluded': False, 'privateRecoveryIncluded': False}
    (destination / 'ZIP_CONTENTS.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: receipt[k] for k in ['filename', 'sha256', 'sizeBytes', 'runtimeDomainCount']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    build(parser.parse_args().output_dir)
