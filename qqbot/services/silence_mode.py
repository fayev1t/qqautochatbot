"""Silence mode management for group conversations."""

from typing import Dict

# Store silence mode state for each group
# Key: group_id, Value: True (silent mode on) or False (silent mode off)
_silence_states: Dict[int, bool] = {}


def is_silent(group_id: int) -> bool:
    """Check if a group is in silence mode.

    Args:
        group_id: QQ group ID

    Returns:
        True if in silence mode, False otherwise
    """
    return _silence_states.get(group_id, False)


def set_silent(group_id: int, silent: bool) -> None:
    """Set silence mode for a group.

    Args:
        group_id: QQ group ID
        silent: True to enable, False to disable
    """
    _silence_states[group_id] = silent

