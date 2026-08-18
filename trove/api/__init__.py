"""REST API layer for Trove (FastAPI).

Thin HTTP facade over the existing services: SessionManager (chat),
CatalogService (database catalog) and KbService (knowledge base /
semantic model). Entry point: `trove serve`.
"""
