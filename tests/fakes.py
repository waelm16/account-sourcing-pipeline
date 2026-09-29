"""Duck-typed fake of hubspot.HubSpot().crm.companies. Records calls, never touches the network."""
from types import SimpleNamespace


def _props_of(obj):
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj.get("properties", obj)
    return getattr(obj, "properties", {}) or {}


def _get(obj, key):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


class FakeCompanies:
    def __init__(self, existing=None):
        # existing: {domain: (id, properties)}
        self.existing = dict(existing or {})
        self.calls = []
        outer = self

        class Search:
            def do_search(self, *a, **kw):
                req = kw.get("public_object_search_request") or (a[0] if a else None)
                outer.calls.append(("search", req))
                values = set()
                for fg in _get(req, "filter_groups") or []:
                    for f in _get(fg, "filters") or []:
                        values.add(str(_get(f, "value") or "").lower())
                values |= {v[4:] for v in values if v.startswith("www.")}
                hits = []
                for dom, (cid, props) in outer.existing.items():
                    if dom in values:
                        p = {"domain": dom, **dict(props)}
                        hits.append(SimpleNamespace(id=cid, properties=p))
                return SimpleNamespace(results=hits, total=len(hits))

        class Basic:
            def create(self, *a, **kw):
                body = kw.get("simple_public_object_input_for_create") or (a[0] if a else None)
                outer.calls.append(("create", _props_of(body)))
                return SimpleNamespace(id="new-1", properties=_props_of(body))

            def update(self, *a, **kw):
                cid = kw.get("company_id") or (a[0] if a else None)
                body = kw.get("simple_public_object_input") or (a[1] if len(a) > 1 else None)
                outer.calls.append(("update", cid, _props_of(body)))
                return SimpleNamespace(id=cid, properties=_props_of(body))

        self.search_api = Search()
        self.basic_api = Basic()

    def writes(self):
        return [c for c in self.calls if c[0] in ("create", "update")]


class FakeProperties:
    """crm.properties.core_api.get_by_name -> object with .options[].value (read-only lookup)."""

    def __init__(self, options):
        self.options = options
        self.calls = []
        outer = self

        class Core:
            def get_by_name(self, object_type=None, property_name=None, **kw):
                outer.calls.append(property_name)
                vals = outer.options.get(property_name, set())
                return SimpleNamespace(name=property_name, options=[SimpleNamespace(value=v) for v in vals])

        self.core_api = Core()


def default_options():
    from contract_spec import HS_ENUMS
    opts = {k: set(v) for k, v in HS_ENUMS.items()}
    opts["sourcing_account_stage"] = {"Sourced", "Qualified", "Researched", "Contacts Selected", "In Outreach"}
    opts["sourcing_sourced_from"] = {"Apollo"}
    return opts


def fake_client(existing=None, options=None):
    comp = FakeCompanies(existing)
    props = FakeProperties(options if options is not None else default_options())
    return SimpleNamespace(crm=SimpleNamespace(companies=comp, properties=props)), comp
