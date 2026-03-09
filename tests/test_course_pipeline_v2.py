from scripts.build_course_pipeline_v2 import build


def test_course_pipeline_v2_bundle_shapes():
    bundles = build()
    bot_ids = [bundle["bot"]["id"] for bundle in bundles]

    assert len(bundles) == 15
    assert len(set(bot_ids)) == 15
    assert "course-intake" in bot_ids
    assert "course-globeiq-importer" in bot_ids

    by_id = {bundle["bot"]["id"]: bundle for bundle in bundles}

    for qc_bot_id in ["course-outline-qc", "course-lesson-qc", "unit-question-bank-qc", "course-final-qc"]:
        output_contract = by_id[qc_bot_id]["bot"]["routing_rules"]["output_contract"]
        assert output_contract["mode"] == "model_output"

    image_trigger = by_id["course-image-planner"]["bot"]["workflow"]["triggers"][0]
    assert image_trigger["join_result_field"] == "source_result.approved_lesson_delivery_package"
    assert image_trigger["join_result_items_alias"] == "approved_lesson_delivery_packages"

    importer = by_id["course-globeiq-importer"]["bot"]
    assert importer["backends"][0]["type"] == "custom"
    assert importer["backends"][0]["provider"] == "http_connection"
    assert importer["routing_rules"]["input_transform"]["enabled"] is True
