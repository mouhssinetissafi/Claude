"""AI / TTS / transcription providers behind small protocols.

Real providers read credentials from the environment. Mock providers are
deterministic and make no network calls; they are selected with
``AUTOEDITOR_MOCK=1`` or ``--mock`` so tests never spend money.
"""
