"""Explicit experimental intervention; legacy experiments used disabled hooks."""
from pathlib import Path

BASELINE_MODES=('disabled_hook','no_hook')


def baseline_mode(config: dict) -> str:
    value=config.get('baseline_mode','disabled_hook')
    if value not in BASELINE_MODES:
        raise ValueError('Unknown baseline mode')
    return value


def hook_mode(record: dict, trial: dict) -> str:
    return 'enabled_hook' if trial['arm']=='optimized' else baseline_mode(record['config'])


def settings_path(folder: Path, record: dict, trial: dict) -> Path:
    return folder/('no-hook-settings.json' if hook_mode(record,trial)=='no_hook' else 'settings.json')
