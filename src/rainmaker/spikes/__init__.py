"""Spike modules: throwaway or evidence-gathering code, dead to the live path.

Nothing under `rainmaker.spikes` is imported by cli.py or any live-path module.
Each spike is a standalone script runnable via `python -m rainmaker.spikes.<name>`.
See the module docstring of each spike for the issue it answers and where its
findings, if any, are written up (usually a docs/architecture/ decision doc).
"""
