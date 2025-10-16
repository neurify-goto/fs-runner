"""Backward-compatible import shim for shared Supabase repository."""

from shared.supabase.repository import JobExecutionRepository

__all__ = ["JobExecutionRepository"]
