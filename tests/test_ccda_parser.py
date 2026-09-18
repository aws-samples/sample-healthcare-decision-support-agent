"""Tests for CCDA parser."""

import pytest
from pathlib import Path

from medical_nudging.parsers.ccda_parser import (
    parse_ccda,
    ParsedCCDA,
    Demographics,
    Problem,
    CCDAParseError,
)


@pytest.fixture
def sample_ccda_xml() -> str:
    """Load sample CCDA XML fixture."""
    fixture_path = Path(__file__).parent / "fixtures" / "sample_ccda.xml"
    return fixture_path.read_text()


@pytest.fixture
def minimal_ccda_xml() -> str:
    """Minimal valid CCDA with only demographics."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <recordTarget>
    <patientRole>
      <patient>
        <name>
          <given>Jane</given>
          <family>Smith</family>
        </name>
        <administrativeGenderCode code="F"/>
        <birthTime value="19900515"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component>
    <structuredBody/>
  </component>
</ClinicalDocument>"""


@pytest.fixture
def partial_ccda_xml() -> str:
    """CCDA with some missing optional sections."""
    return """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <recordTarget>
    <patientRole>
      <patient>
        <name>
          <given>Bob</given>
          <family>Johnson</family>
        </name>
        <administrativeGenderCode code="M"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component>
    <structuredBody>
      <component>
        <section>
          <code code="11450-4" codeSystem="2.16.840.1.113883.6.1" displayName="Problem List"/>
          <title>Problems</title>
          <text><list><item>Diabetes</item></list></text>
          <entry>
            <act classCode="ACT" moodCode="EVN">
              <entryRelationship typeCode="SUBJ">
                <observation classCode="OBS" moodCode="EVN">
                  <value xsi:type="CD" code="44054006" codeSystem="2.16.840.1.113883.6.96" displayName="Diabetes"/>
                </observation>
              </entryRelationship>
            </act>
          </entry>
        </section>
      </component>
    </structuredBody>
  </component>
</ClinicalDocument>"""


def test_parse_valid_ccda(sample_ccda_xml: str) -> None:
    """Test parsing a complete valid CCDA document."""
    result = parse_ccda(sample_ccda_xml)

    assert isinstance(result, ParsedCCDA)

    # Check demographics
    assert result.demographics.name["given"] == "John"
    assert result.demographics.name["family"] == "Doe"
    assert result.demographics.dob == "19800101"
    assert result.demographics.gender == "M"

    # Check problems
    assert len(result.problems) == 1
    assert result.problems[0].description == "Hypertension"
    assert result.problems[0].code == "59621000"

    # Check medications
    assert len(result.medications) == 1
    assert result.medications[0].name == "Lisinopril"
    assert result.medications[0].dosage == "10"
    assert result.medications[0].route == "Oral"

    # Check allergies
    assert len(result.allergies) == 1
    assert result.allergies[0].substance == "Penicillin"
    assert result.allergies[0].reaction == "Hives"
    assert result.allergies[0].severity == "Moderate"

    # Check vitals
    assert len(result.vitals) == 1
    assert result.vitals[0].name == "Systolic Blood Pressure"
    assert result.vitals[0].value == "120"
    assert result.vitals[0].unit == "mmHg"

    # Check immunizations
    assert len(result.immunizations) == 1
    assert result.immunizations[0].vaccine == "Influenza vaccine"
    assert result.immunizations[0].date == "20231015"

    # Check clinical notes
    assert len(result.clinical_notes) > 0


def test_parse_minimal_ccda(minimal_ccda_xml: str) -> None:
    """Test parsing minimal CCDA with only demographics."""
    result = parse_ccda(minimal_ccda_xml)

    assert isinstance(result, ParsedCCDA)
    assert result.demographics.name["given"] == "Jane"
    assert result.demographics.name["family"] == "Smith"
    assert result.demographics.gender == "F"
    assert result.demographics.dob == "19900515"

    # All optional sections should be empty
    assert result.problems == []
    assert result.medications == []
    assert result.allergies == []
    assert result.vitals == []
    assert result.labs == []
    assert result.immunizations == []


def test_parse_partial_ccda(partial_ccda_xml: str) -> None:
    """Test parsing CCDA with some missing optional sections."""
    result = parse_ccda(partial_ccda_xml)

    assert isinstance(result, ParsedCCDA)

    # Demographics present
    assert result.demographics.name["given"] == "Bob"
    assert result.demographics.name["family"] == "Johnson"
    assert result.demographics.dob is None  # Missing DOB

    # Problems present
    assert len(result.problems) == 1
    assert result.problems[0].description == "Diabetes"

    # Other sections missing
    assert result.medications == []
    assert result.allergies == []
    assert result.vitals == []
    assert result.immunizations == []


def test_parse_malformed_xml() -> None:
    """Test that malformed XML raises CCDAParseError."""
    malformed_xml = "<ClinicalDocument>Not closed properly"

    with pytest.raises(CCDAParseError) as exc_info:
        parse_ccda(malformed_xml)

    assert "Invalid XML" in str(exc_info.value)


def test_parse_empty_xml() -> None:
    """Test parsing empty XML string."""
    with pytest.raises(CCDAParseError):
        parse_ccda("")


def test_parse_non_xml_string() -> None:
    """Test parsing non-XML string."""
    with pytest.raises(CCDAParseError):
        parse_ccda("This is not XML at all")


def test_empty_sections_return_empty_lists(minimal_ccda_xml: str) -> None:
    """Test that missing sections return empty lists, not None."""
    result = parse_ccda(minimal_ccda_xml)

    assert result.problems is not None
    assert result.problems == []
    assert result.medications is not None
    assert result.medications == []
    assert result.allergies is not None
    assert result.allergies == []


def test_model_dump() -> None:
    """Test that ParsedCCDA can be serialized to dict using model_dump()."""
    result = ParsedCCDA(
        demographics=Demographics(
            name={"given": "Test", "family": "User"}, dob="20000101", gender="M"
        ),
        problems=[Problem(description="Test Problem", code="123", code_system="SNOMED")],
    )

    data = result.model_dump()
    assert isinstance(data, dict)
    assert data["demographics"]["name"]["given"] == "Test"
    assert len(data["problems"]) == 1
    assert data["problems"][0]["description"] == "Test Problem"


# --- Security Tests ---


def test_xxe_prevention_file_entity() -> None:
    """Test that XXE attacks via file:// entities are prevented.

    Security test: Ensures external entity references don't resolve
    to local files, preventing information disclosure attacks.
    """
    xxe_payload = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE foo [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <recordTarget>
    <patientRole>
      <patient>
        <name>
          <given>&xxe;</given>
          <family>Test</family>
        </name>
      </patient>
    </patientRole>
  </recordTarget>
  <component><structuredBody/></component>
</ClinicalDocument>"""

    # Should parse without error but entity should NOT be resolved
    result = parse_ccda(xxe_payload)

    # The entity reference should either be empty or preserved as-is
    # It should NOT contain file contents like "root:x:0:0"
    given_name = result.demographics.name.get("given", "")
    assert "root:" not in given_name  # Would appear if /etc/passwd was read
    assert "/bin" not in given_name  # Another indicator of file content


def test_xxe_prevention_http_entity() -> None:
    """Test that XXE attacks via http:// entities are prevented.

    Security test: Ensures external entity references don't make
    network requests, preventing SSRF attacks.
    """
    xxe_payload = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE foo [
  <!ENTITY xxe SYSTEM "http://malicious.example.com/steal-data">
]>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <recordTarget>
    <patientRole>
      <patient>
        <name>
          <given>&xxe;</given>
          <family>Test</family>
        </name>
      </patient>
    </patientRole>
  </recordTarget>
  <component><structuredBody/></component>
</ClinicalDocument>"""

    # Should parse without making network requests
    # (no_network=True in parser prevents this)
    result = parse_ccda(xxe_payload)

    # Should complete without error - entity just won't resolve
    assert result.demographics.name.get("family") == "Test"


def test_billion_laughs_prevention() -> None:
    """Test that Billion Laughs (entity expansion) DoS is prevented.

    Security test: Ensures recursive entity expansion attacks
    don't cause memory exhaustion.
    """
    billion_laughs_payload = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <recordTarget>
    <patientRole>
      <patient>
        <name>
          <given>&lol4;</given>
          <family>Test</family>
        </name>
      </patient>
    </patientRole>
  </recordTarget>
  <component><structuredBody/></component>
</ClinicalDocument>"""

    # Should parse without memory explosion
    # (resolve_entities=False and load_dtd=False prevent expansion)
    result = parse_ccda(billion_laughs_payload)

    # Should complete without error - entities just won't expand
    assert result.demographics.name.get("family") == "Test"
