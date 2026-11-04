# contract/ (owner: A)

Data contract v1: Pydantic models (`models.py`), channel registry (`channels.yaml`, the source of
truth, loaded by `registry.py`), human-readable spec (`CONTRACT.md`) and example messages
(`examples/`, valid and invalid). A change bumps `schema_version` and is announced to all three
developers.
