"""Bridges — adapters that feed third-party check outputs into traceguard traces.

Experimental — API may change, not under the frozen 1.0 surface (SPEC §6.6).

Each bridge is opt-in (import the specific submodule) and never pulls its source
system into traceguard's dependency set: bridges duck-type the foreign objects
via ``getattr`` rather than importing them, so traceguard stays dependency-free
and a bridge works even when its source package is not installed.
"""
