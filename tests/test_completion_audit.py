def test_placeholder():
    # The end-to-end audit is exercised against the generated fixed runs; keep a
    # collection test here so the module is imported by the full suite.
    import core.evaluation.completion_audit as audit

    assert callable(audit.run_completion_audit)
