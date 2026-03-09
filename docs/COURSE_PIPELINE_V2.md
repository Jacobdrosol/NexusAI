# Course Pipeline V2

The importable course pipeline bundles live in [`data/course_pipeline_v2`](/Users/jacob/Documents/GitHub/NexusAI/data/course_pipeline_v2).
They are generated from [`scripts/build_course_pipeline_v2.py`](/Users/jacob/Documents/GitHub/NexusAI/scripts/build_course_pipeline_v2.py).

## Design goals

- Strict input and output contracts on every bot.
- Real QC bots that execute model review instead of payload transforms.
- Clean fan-out and join payloads that avoid deep `source_payload.source_payload...` access.
- Deterministic aggregation and packaging stages.
- Explicit retry payloads for outline, lesson, and question-bank revision loops.
- No bundled secrets or private connection exports.

## Stage order

1. `course-intake`
2. `course-outline`
3. `course-outline-qc`
4. `course-unit-builder`
5. `course-lesson-writer`
6. `course-lesson-qc`
7. `course-image-planner`
8. `unit-lesson-aggregator`
9. `unit-question-bank`
10. `unit-question-bank-qc`
11. `course-aggregator`
12. `course-badge-designer`
13. `course-packager`
14. `course-final-qc`
15. `course-globeiq-importer`

## Import notes

- Bundles can be imported in any order, but importing the full set is simpler.
- Exported connections are intentionally omitted from the committed bundles.
- Reattach any required bot-scoped HTTP connection after import.

## Importer requirements

The importer is now a deterministic custom backend that executes attached HTTP connection actions.
It expects two inputs in the launch brief:

- `import_connection_name`
- `platform_import_actions_json`

`platform_import_actions_json` should be a JSON array of action templates. Nested values may use payload expressions such as `{{payload.delivery_package.course_package.course_shell.title}}`.

Example:

```json
[
  {
    "operation_id": "createCourse",
    "body_json": {
      "title": "{{payload.delivery_package.course_package.course_shell.title}}"
    }
  }
]
```

If those values are missing, the importer will fail honestly instead of simulating a successful import.
