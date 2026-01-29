"""Pydantic models for ralph PRD schema."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import AliasChoices, BaseModel, Field


class ItemState(str, Enum):
    """State of a work item."""

    TODO = "todo"
    DOING = "doing"
    DONE = "done"
    BLOCKED = "blocked"


class ItemStatus(BaseModel):
    """Status tracking for a work item."""

    state: ItemState = ItemState.TODO
    attempts: int = 0
    last_error: Optional[str] = None
    done_at: Optional[datetime] = None


class WorkItem(BaseModel):
    """A single work item in the PRD."""

    id: str
    title: str
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    files_hint: list[str] = Field(default_factory=list)
    verify: Optional[list[str]] = None
    status: ItemStatus = Field(default_factory=ItemStatus)


class ProjectMeta(BaseModel):
    """Project metadata."""

    name: str
    language: str = "unknown"
    default_branch: str = "main"


class GlobalConfig(BaseModel):
    """Global configuration including verification commands."""

    verify: list[str] = Field(default_factory=list)


class PRD(BaseModel):
    """Root PRD document schema."""

    model_config = {"populate_by_name": True}

    version: int = 1
    project: ProjectMeta
    global_config: GlobalConfig = Field(
        default_factory=GlobalConfig,
        serialization_alias="global",
        validation_alias=AliasChoices("global", "global_config"),
    )
    items: list[WorkItem] = Field(default_factory=list)

    def get_next_todo(self) -> Optional[WorkItem]:
        """Get the first item with state == todo."""
        for item in self.items:
            if item.status.state == ItemState.TODO:
                return item
        return None

    def get_next_item(self) -> tuple[Optional[WorkItem], bool]:
        """Get the next item to work on.

        Returns (item, is_resuming) where:
        - item: The work item to process, or None if nothing to do
        - is_resuming: True if resuming a "doing" item, False if starting fresh

        Priority:
        1. First item with state == "doing" (resume interrupted work)
        2. First item with state == "todo" (start new work)
        """
        # First, check for any in-progress item to resume
        for item in self.items:
            if item.status.state == ItemState.DOING:
                return item, True

        # Otherwise, pick the first todo item
        for item in self.items:
            if item.status.state == ItemState.TODO:
                return item, False

        return None, False

    def get_item_by_id(self, item_id: str) -> Optional[WorkItem]:
        """Get an item by its ID."""
        for item in self.items:
            if item.id == item_id:
                return item
        return None

    def all_done(self) -> bool:
        """Check if all items are done or blocked."""
        return all(
            item.status.state in (ItemState.DONE, ItemState.BLOCKED)
            for item in self.items
        )

    def count_by_state(self) -> dict[ItemState, int]:
        """Count items by state."""
        counts = {state: 0 for state in ItemState}
        for item in self.items:
            counts[item.status.state] += 1
        return counts
