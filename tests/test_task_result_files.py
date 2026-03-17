from control_plane.task_result_files import extract_file_candidates


def test_extract_file_candidates_from_markdown_deliverable() -> None:
    result = {
        "output": (
            "### Deliverable 1: src/lessonBlocks/MathBlock.tsx\n"
            "```tsx\n"
            "export const MathBlock = () => <div />;\n"
            "```\n"
        )
    }

    candidates = extract_file_candidates(result)

    assert len(candidates) == 1
    assert candidates[0]["path"] == "src/lessonBlocks/MathBlock.tsx"
    assert "MathBlock" in candidates[0]["content"]
    assert candidates[0]["source"] == "markdown_code_fence"


def test_extract_file_candidates_prefers_explicit_artifacts_for_same_path() -> None:
    result = {
        "output": (
            "File: src/lessonBlocks/MathBlock.tsx\n"
            "```tsx\n"
            "export const wrongValue = 1;\n"
            "```\n"
        ),
        "artifacts": [
            {
                "label": "MathBlock.tsx",
                "path": "src/lessonBlocks/MathBlock.tsx",
                "content": "export const rightValue = 2;\n",
            }
        ],
    }

    candidates = extract_file_candidates(result)

    assert len(candidates) == 1
    assert candidates[0]["path"] == "src/lessonBlocks/MathBlock.tsx"
    assert candidates[0]["content"] == "export const rightValue = 2;\n"
    assert candidates[0]["source"] == "explicit_artifact"


def test_extract_file_candidates_ignores_prose_before_code_fence() -> None:
    result = {
        "output": (
            "# Geometry Lesson Blocks\n"
            "\n"
            "The primary interface for creating geometric entities.\n"
            "```typescript\n"
            "interface GeometryBlock { render(): void }\n"
            "```\n"
            "\n"
            "Deliverable: docs/geometry_lesson_blocks.md\n"
            "```markdown\n"
            "# Geometry Lesson Blocks\n"
            "```\n"
        )
    }

    candidates = extract_file_candidates(result)

    assert len(candidates) == 1
    assert candidates[0]["path"] == "docs/geometry_lesson_blocks.md"
