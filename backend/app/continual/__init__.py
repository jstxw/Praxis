"""Continual harness: H = (I, S, M, V, C), persisted across tasks.

The middle layer of ARCHITECTURE §1. Harness content (instructions,
skills, verification, control) is immutable per version; memory is a
separately versioned axis that is frozen during any comparison.
"""
