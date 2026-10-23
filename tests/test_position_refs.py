from app.exam import fix_position_refs, normalize_mcq


def test_fix_position_refs_ordinal_and_letter():
    opts = ["Mitochondria", "Stroma", "Thylakoid membranes", "Nucleus"]
    out = fix_position_refs("This makes the first option correct, not option C.", opts)
    assert '"Mitochondria"' in out and '"Thylakoid membranes"' in out
    assert "first option" not in out and "option C" not in out


def test_normalize_mcq_explanation_matches_shuffled_answer():
    q = {"question": "Where do the light-dependent reactions occur?",
         "options": ["Thylakoid membranes", "Stroma", "Mitochondria", "Nucleus"], "answer": 0,
         "explanation": "The first option is correct because p. 1 says so; option B hosts the Calvin cycle.",
         "pages": [1]}
    n = normalize_mcq(q, 1, 2, {1: "text"})
    assert n["options"][n["answer"]] == "Thylakoid membranes"
    assert '"Thylakoid membranes" is correct' in n["explanation"].replace('The "', '"')
    assert '"Stroma"' in n["explanation"]
