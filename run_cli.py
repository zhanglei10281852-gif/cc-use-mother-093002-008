import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from integration_pilot.contracts import ComponentArtifact, PilotSiteRecord

entity = ComponentArtifact("E-DEMO", "跨境教育技术集成试点", 1)
record = PilotSiteRecord("R-DEMO", entity.entity_id, "已登记")
print(json.dumps({"entity": entity.display_name, "revision": entity.revision, "record_state": record.category}, ensure_ascii=False))
