# `lists`

Candidate universes and helper scripts for screening runs.

This folder defines which items the pipelines operate on. In practice, the quality of the screening depends heavily on the quality of these universes.

## Purpose

These lists are used to:

- define broad vs narrow screening scope
- separate homogeneous-style and skin-heavy universes
- create smaller operational batches
- keep experiment inputs explicit and versioned

## Main Files

- [`screening_full.py`](./screening_full.py)
  Curated mixed universe for normal screening.
- [`screening_sub.py`](./screening_sub.py)
  Small shortlist for focused or quick runs.
- [`screening_super_full.py`](./screening_super_full.py)
  Very broad item universe.
- [`screening_steam_listing.py`](./screening_steam_listing.py)
  Candidate list for Steam listing-level collection.
- [`homogenous_no_stickers_patches.py`](./homogenous_no_stickers_patches.py)
  Auto-generated list intended to isolate more homogeneous items.
- [`skins_normal.py`](./skins_normal.py)
  Auto-generated skin-only style list.
- [`item_sublist_creator.py`](./item_sublist_creator.py)
  Helper script for generating filtered lists from a source universe.

## Role In The Project

Most pipelines in this repository do not discover items by themselves. They assume the item universe is already defined here.

So this folder is the control plane for:

- what gets screened
- how large a run is
- whether a run is oriented toward homogeneous items, skins, or a mixed universe
