"""Example usage of the Medical Nudging Orchestrator.

This demonstrates the end-to-end flow:
1. Parse CCDA XML
2. Generate patient summary
3. Retrieve clinical guidelines
4. Generate actionable nudges
"""

from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from medical_nudging.agents.orchestrator import MedicalNudgingOrchestrator

console = Console()


def main() -> None:
    """Run orchestrator example."""
    # Load sample CCDA
    ccda_path = Path(__file__).parent.parent / "tests" / "fixtures" / "sample_ccda.xml"
    ccda_xml = ccda_path.read_text()

    # Define visit context
    visit_context = {
        "visit_type": "ambulatory",
        "specialty": "cardiology",
        "chief_complaint": "Routine follow-up for hypertension",
    }

    # Create orchestrator
    orchestrator = MedicalNudgingOrchestrator(specialty="cardiology")

    # Generate nudges
    console.print("[bold blue]Generating clinical nudges...[/bold blue]")
    console.rule()

    response = orchestrator.generate_nudges(patient_data=ccda_xml, visit_context=visit_context)

    # Display status
    status_color = {"success": "green", "partial": "yellow", "error": "red"}
    console.print(f"\nStatus: [{status_color[response.status]}]{response.status}[/]")

    if response.warnings:
        console.print(f"[yellow]Warnings: {', '.join(response.warnings)}[/yellow]")

    if response.error:
        console.print(f"[red]Error: {response.error}[/red]")
        return

    # Patient Summary
    console.print(
        Panel(response.patient_summary or "", title="Patient Summary", border_style="blue")
    )

    # Clinical Nudges
    console.print(f"\n[bold]Clinical Nudges ({len(response.nudges)} found)[/bold]")

    for i, nudge in enumerate(response.nudges, 1):
        urgency_color = {"urgent": "red", "warning": "yellow", "informational": "blue"}

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Field", style="dim")
        table.add_column("Value")

        table.add_row("Urgency", f"[{urgency_color[nudge.urgency]}]{nudge.urgency.upper()}[/]")
        table.add_row("Category", nudge.category)
        table.add_row("Action", nudge.action_type)
        table.add_row("Description", nudge.description)
        table.add_row("Rationale", nudge.rationale)

        if nudge.guideline_citation:
            table.add_row(
                "Citation",
                f"{nudge.guideline_citation.source} - {nudge.guideline_citation.section}",
            )

        if nudge.icd_codes:
            table.add_row("ICD-10", ", ".join(nudge.icd_codes))
        if nudge.cpt_codes:
            table.add_row("CPT", ", ".join(nudge.cpt_codes))

        console.print(Panel(table, title=f"{i}. {nudge.title}", border_style="cyan"))

    # Metadata
    meta_table = Table(show_header=False, box=None)
    meta_table.add_column("Field", style="dim")
    meta_table.add_column("Value")
    meta_table.add_row("Model", response.metadata.model_version)
    meta_table.add_row("Processing Time", f"{response.metadata.processing_time_ms}ms")
    if response.metadata.guidelines_used:
        meta_table.add_row("Guidelines", ", ".join(response.metadata.guidelines_used))

    console.print(Panel(meta_table, title="Metadata", border_style="dim"))


if __name__ == "__main__":
    main()
