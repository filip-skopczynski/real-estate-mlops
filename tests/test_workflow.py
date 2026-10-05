"""Execute the workflow readiness script against first-snapshot and historical data."""

from pathlib import Path

import nbformat
import pytest
import yaml

from tests.demo_data import demo_observations

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('enough_history', [False, True])
def test_scheduler_readiness_is_safe_during_bootstrap(tmp_path, monkeypatch, enough_history):
    workflow = yaml.load((ROOT / '.github/workflows/scraper_pipeline.yml').read_text(), Loader=yaml.BaseLoader)
    step = next(step for step in workflow['jobs']['pipeline']['steps'] if step.get('id') == 'readiness')
    python_script = step['run'].split("python - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    observations = demo_observations().iloc[:180].copy()
    if not enough_history:
        observations['observed_at'] = '2026-09-01T08:00:00Z'
    (tmp_path / 'data').mkdir()
    observations.to_csv(tmp_path / 'data/training.csv', index=False)
    output = tmp_path / 'github_output'
    summary = tmp_path / 'github_summary'
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('TRAIN_SPLIT', 'temporal')
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(summary))
    exec(compile(python_script, 'workflow-readiness', 'exec'), {})
    assert output.read_text().strip() == 'ready=' + str(enough_history).lower()
    if not enough_history:
        assert 'Observations saved' in summary.read_text()
    assert len(observations) == 180


def test_eda_notebook_is_valid_and_has_no_saved_data():
    notebook = nbformat.read(ROOT / 'notebooks/01_eda.ipynb', as_version=4)
    nbformat.validate(notebook)
    code_cells = [cell for cell in notebook.cells if cell.cell_type == 'code']
    assert code_cells
    for cell in code_cells:
        compile(cell.source, 'notebook-cell', 'exec')
        assert not cell.outputs
