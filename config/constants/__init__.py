"""config/constants — shared enums and constants across the Atlas backend.

Permission keys, status values, and other cross-cutting constants live here
so they are imported by both the seed (database/seeds/) and the enforcement
layer (backend/app/core/rbac.py) with no string-literal drift.
"""
