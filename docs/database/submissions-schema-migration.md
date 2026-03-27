# AI Project Submissions Schema Migration

This document describes the database schema migrations for the AI Project Submissions and Grading feature.

## Overview

The submissions schema enables tracking student submissions, AI-graded results, and feedback for educational projects within the NexusAI platform.

## Migration History

### Initial Schema (2025-01-15)

**Migration ID:** `20250115000000_AddAiProjectGradingSchema`

**Description:** Add tables and columns for AI project submission tracking and grading.

## Schema Changes

### New Tables

#### `submissions`

Stores student project submissions.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `id` | TEXT | No | - | Primary key (UUID) |
| `project_id` | TEXT | No | - | Reference to project |
| `user_id` | TEXT | No | - | Student/user who submitted |
| `assignment_id` | TEXT | No | - | Assignment reference |
| `submitted_at` | TEXT | No | - | ISO 8601 timestamp |
| `file_paths` | TEXT | Yes | NULL | JSON array of file paths |
| `status` | TEXT | No | `'submitted'` | submission status |
| `grade_id` | TEXT | Yes | NULL | Reference to grading result |
| `created_at` | TEXT | No | - | Creation timestamp |
| `updated_at` | TEXT | No | - | Last update timestamp |

#### `gradings`

Stores AI-generated grading results.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `id` | TEXT | No | - | Primary key (UUID) |
| `submission_id` | TEXT | No | - | Reference to submission |
| `grade` | REAL | No | - | Numeric score |
| `max_grade` | REAL | No | `100.0` | Maximum possible score |
| `feedback_summary` | TEXT | Yes | NULL | Overall feedback |
| `feedback_strengths` | TEXT | Yes | NULL | JSON array of strengths |
| `feedback_improvements` | TEXT | Yes | NULL | JSON array of improvements |
| `rubric_breakdown` | TEXT | Yes | NULL | JSON rubric scores |
| `class_comparison` | TEXT | Yes | NULL | JSON class stats |
| `ai_suggestions` | TEXT | Yes | NULL | JSON AI suggestions |
| `graded_at` | TEXT | No | - | Grading timestamp |
| `graded_by_model` | TEXT | Yes | NULL | Model used for grading |
| `created_at` | TEXT | No | - | Creation timestamp |

#### `submission_artifacts`

Stores metadata about submitted files and artifacts.

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `id` | TEXT | No | - | Primary key (UUID) |
| `submission_id` | TEXT | No | - | Reference to submission |
| `artifact_type` | TEXT | No | - | Type: `code`, `document`, `media` |
| `file_name` | TEXT | No | - | Original file name |
| `file_path` | TEXT | No | - | Storage path |
| `file_size` | INTEGER | Yes | NULL | Size in bytes |
| `checksum` | TEXT | Yes | NULL | SHA-256 hash |
| `created_at` | TEXT | No | - | Creation timestamp |

### Indexes

```sql
CREATE INDEX idx_submissions_project_id ON submissions(project_id);
CREATE INDEX idx_submissions_user_id ON submissions(user_id);
CREATE INDEX idx_submissions_assignment_id ON submissions(assignment_id);
CREATE INDEX idx_submissions_status ON submissions(status);
CREATE INDEX idx_gradings_submission_id ON gradings(submission_id);
CREATE INDEX idx_submission_artifacts_submission_id ON submission_artifacts(submission_id);
```

## Migration Implementation

The migration uses the additive-only schema migration system provided by `SchemaManager` and `DatabaseEngineer`.

### Migration Plan Example

```python
from control_plane.database.schema_manager import (
    SchemaManager,
    TableDefinition,
    ColumnDefinition,
)

# Define submissions table
submissions_table = TableDefinition(
    name="submissions",
    columns=[
        ColumnDefinition(name="id", type="TEXT", primary_key=True),
        ColumnDefinition(name="project_id", type="TEXT", nullable=False),
        ColumnDefinition(name="user_id", type="TEXT", nullable=False),
        ColumnDefinition(name="assignment_id", type="TEXT", nullable=False),
        ColumnDefinition(name="submitted_at", type="TEXT", nullable=False),
        ColumnDefinition(name="file_paths", type="TEXT", nullable=True),
        ColumnDefinition(name="status", type="TEXT", nullable=False, default="'submitted'"),
        ColumnDefinition(name="grade_id", type="TEXT", nullable=True),
        ColumnDefinition(name="created_at", type="TEXT", nullable=False),
        ColumnDefinition(name="updated_at", type="TEXT", nullable=False),
    ],
)

# Create migration plan
plan = await database_engineer.plan_migration(
    plan_id="20250115000000_AddAiProjectGradingSchema",
    description="Add AI project submission and grading schema",
    tables=[submissions_table, gradings_table, submission_artifacts_table],
    indexes=[
        "CREATE INDEX idx_submissions_project_id ON submissions(project_id)",
        "CREATE INDEX idx_submissions_user_id ON submissions(user_id)",
        "CREATE INDEX idx_gradings_submission_id ON gradings(submission_id)",
    ],
)

# Apply migration
result = await database_engineer.apply_migration(plan)
```

## Rollback Procedure

If rollback is required, note that the migration system only supports additive changes. To rollback:

1. Create a new migration that adds replacement tables with `_backup` suffix
2. Migrate data manually if needed
3. The original tables remain but should not be used by new code

## Data Integrity

- All foreign key relationships are application-level (SQLite does not enforce by default)
- Timestamps are stored as ISO 8601 strings in UTC
- JSON columns should be validated at the application layer

## Related Documentation

- [AI Project Submissions Feature](../features/ai-project-submissions.md)
- [Database Engineer API](./database-engineer-api.md)
- [Schema Manager](./schema-manager.md)
