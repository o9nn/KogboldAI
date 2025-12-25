"""
KoboldAI Kernel Integration Module

This module provides the integration layer between the Python server (aiserver.py)
and the high-performance C kernel. It automatically falls back to Python implementations
when the kernel is not available.

Usage:
    from kernel_integration import KernelManager, get_kernel_manager

    # Get the singleton kernel manager
    km = get_kernel_manager()

    # Check if kernel is available
    if km.is_available():
        # Use kernel-accelerated operations
        km.create_story()
        km.add_worldinfo(keywords="dragon", content="A fearsome creature")
        matched = km.scan_worldinfo("The dragon appeared")
"""

import os
import logging
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field
from threading import Lock

logger = logging.getLogger(__name__)

# Try to import the kernel FFI
try:
    import kobold_kernel_ffi as kernel
    KERNEL_FFI_AVAILABLE = True
except ImportError:
    KERNEL_FFI_AVAILABLE = False
    kernel = None


@dataclass
class KernelStats:
    """Kernel performance and memory statistics"""
    available: bool = False
    version: str = "unavailable"
    initialized: bool = False
    total_bytes: int = 0
    used_bytes: int = 0
    peak_bytes: int = 0
    num_allocations: int = 0
    operations_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "version": self.version,
            "initialized": self.initialized,
            "memory": {
                "total_bytes": self.total_bytes,
                "used_bytes": self.used_bytes,
                "peak_bytes": self.peak_bytes,
                "num_allocations": self.num_allocations,
            },
            "operations_count": self.operations_count,
        }


@dataclass
class WorldInfoEntryWrapper:
    """Wrapper for world info entries that works with or without kernel"""
    uid: int
    keywords: str
    content: str
    selective: bool = True
    constant: bool = False
    _kernel_handle: Any = None

    def matches(self, context: str) -> bool:
        """Check if this entry matches the given context"""
        if self._kernel_handle is not None:
            return self._kernel_handle.matches(context)

        # Python fallback: simple keyword matching
        context_lower = context.lower()
        for kw in self.keywords.split(","):
            kw = kw.strip().lower()
            if kw and kw in context_lower:
                return True
        return False


class KernelManager:
    """
    Singleton manager for the KoboldAI kernel.

    Provides a high-level interface for kernel operations with automatic
    fallback to Python implementations when the kernel is unavailable.
    """

    _instance: Optional['KernelManager'] = None
    _lock = Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._initialized = True
        self._kernel_available = False
        self._kernel_initialized = False
        self._story = None
        self._worldinfo_entries: Dict[int, WorldInfoEntryWrapper] = {}
        self._next_uid = 0
        self._operations_count = 0
        self._stats = KernelStats()

        # Try to load and initialize the kernel
        self._try_load_kernel()

    def _try_load_kernel(self, memory_mb: int = 256) -> bool:
        """Attempt to load and initialize the kernel"""
        if not KERNEL_FFI_AVAILABLE:
            logger.info("Kernel FFI module not available. Using Python fallback.")
            return False

        try:
            if kernel.is_available():
                if kernel.init(memory_mb):
                    self._kernel_available = True
                    self._kernel_initialized = True
                    self._stats.available = True
                    self._stats.version = kernel.get_version()
                    self._stats.initialized = True
                    logger.info(f"KoboldAI kernel initialized (version: {self._stats.version})")
                    return True
                else:
                    logger.warning("Kernel init() failed. Using Python fallback.")
            else:
                logger.info("Kernel library not found. Using Python fallback.")
        except Exception as e:
            logger.error(f"Error initializing kernel: {e}")

        return False

    def is_available(self) -> bool:
        """Check if the kernel is available and initialized"""
        return self._kernel_available and self._kernel_initialized

    def get_stats(self) -> KernelStats:
        """Get current kernel statistics"""
        if self.is_available():
            mem_stats = kernel.get_memory_stats()
            self._stats.total_bytes = mem_stats.get("total_bytes", 0)
            self._stats.used_bytes = mem_stats.get("used_bytes", 0)
            self._stats.peak_bytes = mem_stats.get("peak_bytes", 0)
            self._stats.num_allocations = mem_stats.get("num_allocations", 0)

        self._stats.operations_count = self._operations_count
        return self._stats

    def create_story(self) -> bool:
        """Create a new story context"""
        self._operations_count += 1

        if self.is_available():
            try:
                self._story = kernel.Story()
                return True
            except Exception as e:
                logger.error(f"Kernel story creation failed: {e}")

        # Python fallback: just mark that we have a story context
        self._story = {"memory": "", "authors_note": "", "chunks": []}
        return True

    def set_memory(self, memory_text: str) -> bool:
        """Set the persistent memory text"""
        self._operations_count += 1

        if self.is_available() and isinstance(self._story, kernel.Story):
            return self._story.set_memory(memory_text)

        # Python fallback
        if isinstance(self._story, dict):
            self._story["memory"] = memory_text
            return True
        return False

    def set_authors_note(self, note_text: str) -> bool:
        """Set the author's note text"""
        self._operations_count += 1

        if self.is_available() and isinstance(self._story, kernel.Story):
            return self._story.set_authors_note(note_text)

        # Python fallback
        if isinstance(self._story, dict):
            self._story["authors_note"] = note_text
            return True
        return False

    def add_worldinfo(
        self,
        keywords: str,
        content: str,
        selective: bool = True,
        constant: bool = False
    ) -> int:
        """
        Add a world info entry.

        Returns:
            Unique ID for the entry
        """
        self._operations_count += 1
        uid = self._next_uid
        self._next_uid += 1

        wrapper = WorldInfoEntryWrapper(
            uid=uid,
            keywords=keywords,
            content=content,
            selective=selective,
            constant=constant,
        )

        if self.is_available():
            try:
                entry = kernel.WorldInfoEntry(
                    keywords=keywords,
                    content=content,
                    selective=selective,
                    constant=constant,
                )
                wrapper._kernel_handle = entry

                if isinstance(self._story, kernel.Story):
                    self._story.add_worldinfo(entry)
            except Exception as e:
                logger.error(f"Kernel world info creation failed: {e}")

        self._worldinfo_entries[uid] = wrapper
        return uid

    def remove_worldinfo(self, uid: int) -> bool:
        """Remove a world info entry by UID"""
        if uid in self._worldinfo_entries:
            del self._worldinfo_entries[uid]
            return True
        return False

    def scan_worldinfo(self, context: str) -> List[Tuple[int, str]]:
        """
        Scan context for matching world info entries.

        Returns:
            List of (uid, content) tuples for matching entries
        """
        self._operations_count += 1
        matches = []

        for uid, entry in self._worldinfo_entries.items():
            # Constant entries always match
            if entry.constant:
                matches.append((uid, entry.content))
            # Selective entries only match if keywords found
            elif entry.selective and entry.matches(context):
                matches.append((uid, entry.content))

        return matches

    def get_worldinfo_content(self, context: str) -> str:
        """
        Get combined content of all matching world info entries.

        This is a convenience method that returns the concatenated content
        of all matching entries, suitable for injection into the context.
        """
        matches = self.scan_worldinfo(context)
        if not matches:
            return ""

        return "\n".join(content for _, content in matches)

    def clear_worldinfo(self):
        """Clear all world info entries"""
        self._worldinfo_entries.clear()
        self._next_uid = 0

    def shutdown(self):
        """Shutdown the kernel and free resources"""
        if self.is_available():
            try:
                # Clear references to kernel objects first
                self._story = None
                for entry in self._worldinfo_entries.values():
                    entry._kernel_handle = None
                self._worldinfo_entries.clear()

                kernel.shutdown()
                logger.info("Kernel shutdown complete")
            except Exception as e:
                logger.error(f"Error during kernel shutdown: {e}")

        self._kernel_initialized = False
        self._stats.initialized = False


# Singleton accessor
_kernel_manager: Optional[KernelManager] = None

def get_kernel_manager() -> KernelManager:
    """Get the singleton KernelManager instance"""
    global _kernel_manager
    if _kernel_manager is None:
        _kernel_manager = KernelManager()
    return _kernel_manager


def initialize_kernel(memory_mb: int = 256) -> bool:
    """
    Initialize the kernel with the specified memory pool size.

    This is a convenience function that initializes the singleton
    KernelManager if it hasn't been initialized yet.

    Args:
        memory_mb: Memory pool size in megabytes

    Returns:
        True if kernel is available and initialized
    """
    km = get_kernel_manager()
    return km.is_available()


def get_kernel_status() -> Dict[str, Any]:
    """
    Get kernel status information for display in UI.

    Returns:
        Dictionary with kernel status information
    """
    km = get_kernel_manager()
    stats = km.get_stats()

    return {
        "enabled": km.is_available(),
        "version": stats.version,
        "memory_used_mb": stats.used_bytes / (1024 * 1024) if stats.used_bytes else 0,
        "memory_total_mb": stats.total_bytes / (1024 * 1024) if stats.total_bytes else 0,
        "operations": stats.operations_count,
        "status": "active" if km.is_available() else "fallback",
    }


# Module-level initialization check
if KERNEL_FFI_AVAILABLE:
    logger.debug("Kernel FFI module loaded successfully")
else:
    logger.debug("Kernel FFI module not available - will use Python fallback")
