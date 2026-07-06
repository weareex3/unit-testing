"""Generate a ~50-scenario Employee Central stress-test workbook in the exact
format engine/parser.py expects (columns: Row, Script ID, Scenario Name, Role,
Step, Action, Test Data, Expected Result, Status, Comments; sheet name contains
'End-to-End'; name/role only on a scenario's first row).

~42 realistic EC scenarios + edge cases designed to probe the parser (section
headers, blank rows, unicode, single-step, missing expected, PRE-REQ rows).

Run:  python -m eval.gen_ec_scripts
Writes: scripts/EX3_EC_Stress50_V1.xlsx
"""

from pathlib import Path
import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "scripts" / "EX3_EC_Stress50_V1.xlsx"

HEADER = ["Row", "Script ID", "Scenario Name", "Role", "Step", "Action",
          "Test Data", "Expected Result", "Status", "Comments"]

# (script_id, name, role, [(action, test_data, expected), ...])
S = []

def sc(sid, name, role, steps):
    S.append((sid, name, role, steps))

# ── Hire / lifecycle ────────────────────────────────────────────────────────
sc("EC-HIRE-301", "Process New Hire", "HR Administrator", [
    ("Open the New Hire Wizard from Admin Centre", "", "Wizard opens on Step 1"),
    ("Enter personal information", "Name: Aisha Khan", "Personal info saved"),
    ("Set employment details: position, start date, manager", "Start: 01/07/2026", "Employment details saved"),
    ("Enter compensation and pay component", "Salary: 52000 GBP", "Compensation captured"),
    ("Submit and confirm the new hire", "", "Employee ID generated")])
sc("EC-REHIRE-301", "Rehire a Former Employee", "HR Administrator", [
    ("Search for the inactive former employee", "Employee: Mark Webb", "Former record found"),
    ("Initiate Rehire action", "", "Rehire wizard opens"),
    ("Confirm new start date and position", "Start: 14/07/2026", "Rehire details saved"),
    ("Submit the rehire", "", "Employee reactivated")])
sc("EC-TERM-301", "Terminate an Employee", "HR Administrator", [
    ("Open the employee record", "Employee: John Doe", "Record opens"),
    ("Select Take Action then Terminate", "", "Termination form opens"),
    ("Enter termination reason and last day worked", "Reason: Resignation", "Details captured"),
    ("Submit termination for approval", "", "Workflow triggered")])
sc("EC-PROBATION-301", "Complete Probation Review", "Line Manager", [
    ("Open the employee's Job Information", "Employee: Sara Lee", "Job Info displayed"),
    ("Edit probation status to Passed", "Status: Passed", "Probation updated"),
    ("Save the change with effective date", "", "Change saved")])

# ── Job / org / position movements ──────────────────────────────────────────
sc("EC-TRANSFER-301", "Transfer Employee to New Department", "HR Administrator", [
    ("Open the employee People Profile", "Employee: Priya Nair", "Profile opens"),
    ("Take Action then Transfer", "", "Transfer form opens"),
    ("Change department and cost center", "Dept: Finance EMEA", "New org captured"),
    ("Submit the transfer", "", "Transfer recorded")])
sc("EC-PROMO-301", "Promote Employee", "HR Administrator", [
    ("Open the employee Job Information card", "Employee: Tom Hardy", "Card opens"),
    ("Edit job title and pay grade", "Title: Senior Analyst", "Job info updated"),
    ("Apply a salary increase", "Salary: +8%", "Compensation updated"),
    ("Submit promotion for approval", "", "Approval requested")])
sc("EC-DEMOTE-301", "Demote Employee", "HR Administrator", [
    ("Open the employee Job Information", "Employee: Greg Pitt", "Job Info opens"),
    ("Lower the pay grade and title", "Grade: P2", "Job info updated"),
    ("Submit the change", "", "Change saved")])
sc("EC-MGRCHANGE-301", "Change Reporting Manager", "HR Administrator", [
    ("Open the employee Job Relationships", "Employee: Nina Roy", "Relationships shown"),
    ("Edit the line manager field", "Manager: David Cole", "Manager updated"),
    ("Save with effective date", "Effective: {{today}}", "Saved")])
sc("EC-POS-CREATE-301", "Create a Position", "Position Manager", [
    ("Navigate to Manage Positions", "", "Position list loads"),
    ("Click Create New Position", "", "Position form opens"),
    ("Enter title, department and FTE", "Title: HR Advisor", "Position created"),
    ("Save the new position", "", "Position ID generated")])
sc("EC-POS-RETIRE-301", "Retire a Position", "Position Manager", [
    ("Search for the position to retire", "Position: POS100912", "Position found"),
    ("Set status to To Be Retired", "", "Status updated"),
    ("Save the change", "", "Position retired")])
sc("EC-ORG-301", "Create Organisation Unit", "HR Administrator", [
    ("Navigate to Manage Organization, Pay and Job Structures", "", "Foundation page loads"),
    ("Create a new Department object", "Dept: Customer Success", "Object form opens"),
    ("Enter name and effective date and save", "", "Department created")])
sc("EC-COSTCTR-301", "Assign Cost Center", "Finance Partner", [
    ("Open the employee Job Information", "Employee: Liam Ford", "Job Info opens"),
    ("Edit cost center assignment", "Cost Center: CC-4400", "Cost center set"),
    ("Save the change", "", "Saved")])

# ── Compensation ────────────────────────────────────────────────────────────
sc("EC-COMP-301", "Update Base Salary", "Compensation Manager", [
    ("Search and open the employee", "Employee: Eva Stone", "Record opens"),
    ("Open the Compensation Information block", "", "Compensation shown"),
    ("Enter the new annual salary", "Salary: 61000", "New salary entered"),
    ("Save and confirm", "", "Compensation updated")])
sc("EC-BONUS-301", "Add One-Time Bonus", "Compensation Manager", [
    ("Open the employee compensation", "Employee: Eva Stone", "Compensation opens"),
    ("Add a Spot Bonus pay component", "Amount: 2000", "Component added"),
    ("Save the change", "", "Bonus recorded")])
sc("EC-PAYCOMP-301", "Edit Recurring Pay Component", "Payroll Administrator", [
    ("Open the employee Compensation Information", "Employee: Raj Patel", "Block opens"),
    ("Edit the Car Allowance amount", "Amount: 450/month", "Component updated"),
    ("Save with effective date", "", "Saved")])

# ── Personal / biographical data ────────────────────────────────────────────
sc("EC-PERSONAL-301", "Edit Personal Information", "Employee (ESS)", [
    ("Open My Profile then Personal Information", "", "Personal info shown"),
    ("Edit marital status", "Status: Married", "Field updated"),
    ("Save the change", "", "Saved")])
sc("EC-NAME-301", "Legal Name Change", "HR Administrator", [
    ("Open the employee Personal Information", "Employee: Anna Bell", "Block opens"),
    ("Edit the legal last name", "Last name: Bell-Hughes", "Name updated"),
    ("Attach supporting document and save", "", "Change saved")])
sc("EC-ADDR-301", "Update Home Address", "Employee (ESS)", [
    ("Open My Profile then Addresses card", "", "Addresses card opens"),
    ("Edit the home address line and postcode", "Postcode: BN1 4AA", "Address updated"),
    ("Save the change", "", "Saved")])
sc("EC-CONTACT-301", "Update Emergency Contact", "Employee (ESS)", [
    ("Open Personal Information then Emergency Contacts", "", "Contacts shown"),
    ("Add a new emergency contact", "Contact: Sam Bell", "Contact added"),
    ("Save the change", "", "Saved")])
sc("EC-NATID-301", "Add National ID", "HR Administrator", [
    ("Open the employee Biographical Information", "Employee: Omar Aziz", "Block opens"),
    ("Add a National ID entry", "NI: AB123456C", "ID captured"),
    ("Save the change", "", "Saved")])
sc("EC-BANK-301", "Update Bank Details", "Payroll Administrator", [
    ("Open the employee Payment Information", "Employee: Mia Chen", "Payment block opens"),
    ("Edit the bank account number and sort code", "Sort: 20-00-00", "Bank details updated"),
    ("Save and confirm", "", "Saved")])
sc("EC-DEPENDENT-301", "Add a Dependent", "Employee (ESS)", [
    ("Open Personal Information then Dependents", "", "Dependents shown"),
    ("Add a child dependent", "Name: Leo Chen", "Dependent added"),
    ("Save the change", "", "Saved")])
sc("EC-WORKPERMIT-301", "Record Work Permit", "HR Administrator", [
    ("Open the employee Work Permit Info", "Employee: Yuki Sato", "Block opens"),
    ("Add a work permit with expiry date", "Expiry: 31/12/2027", "Permit recorded"),
    ("Save the change", "", "Saved")])

# ── Time / ESS / MSS ────────────────────────────────────────────────────────
sc("EC-TIMEOFF-REQ-301", "Request Time Off", "Employee (ESS)", [
    ("Open Time Off then Request", "", "Request form opens"),
    ("Select dates and absence type", "Type: Annual Leave", "Request populated"),
    ("Submit the request", "", "Request sent for approval")])
sc("EC-TIMEOFF-APP-301", "Approve Time Off", "Line Manager", [
    ("Open the Approvals to-do tile", "", "Pending approvals shown"),
    ("Open the time off request", "Requester: Aisha Khan", "Request details shown"),
    ("Approve the request", "", "Request approved")])
sc("EC-ESS-PROFILE-301", "Update ESS Profile Photo", "Employee (ESS)", [
    ("Open My Profile", "", "Profile opens"),
    ("Upload a new profile photo", "File: photo.jpg", "Photo updated")])
sc("EC-MSS-TEAM-301", "View Team in MSS", "Line Manager", [
    ("Open My Team tile", "", "Team view loads"),
    ("Open a direct report's profile", "Report: Sara Lee", "Profile opens")])

# ── Workflow / proxy / admin ────────────────────────────────────────────────
sc("EC-PROXY-301", "Proxy as Another User", "HR Administrator", [
    ("Open the user menu and choose Proxy Now", "", "Proxy dialog opens"),
    ("Search for and select the target user", "Employee: Jane Smith", "Target selected"),
    ("Confirm to start the proxy session", "", "Proxy session active")])
sc("EC-WF-DELEGATE-301", "Delegate Workflows", "Line Manager", [
    ("Open Delegate Workflows tile", "", "Delegation form opens"),
    ("Select a delegate and date range", "Delegate: David Cole", "Delegation set"),
    ("Save the delegation", "", "Saved")])
sc("EC-WF-APPROVE-301", "Approve a Pending Workflow", "HR Manager", [
    ("Open Pending Workflows", "", "Workflow list shown"),
    ("Open a data change request", "", "Request details shown"),
    ("Approve and add a comment", "Comment: Verified", "Workflow approved")])
sc("EC-IMPORT-301", "Import Employee Data", "HR Administrator", [
    ("Navigate to Import and Export Data", "", "Import page loads"),
    ("Select the import template and file", "File: addresses.csv", "File staged"),
    ("Validate and run the import", "", "Records imported")])
sc("EC-MASS-301", "Run a Mass Change", "HR Administrator", [
    ("Open Manage Mass Changes", "", "Mass change wizard opens"),
    ("Select affected population and field", "Field: Location", "Population selected"),
    ("Preview and execute the change", "", "Mass change applied")])
sc("EC-PENDING-301", "Process Pending Hire", "HR Administrator", [
    ("Open the Pending Hires tile", "", "Pending hires listed"),
    ("Select a candidate to convert", "Candidate: Noah Reed", "Hire wizard opens"),
    ("Complete and submit the hire", "", "Employee created")])

# ── Foundation / config / reporting ─────────────────────────────────────────
sc("EC-WORKFLOW-CFG-301", "Configure a Workflow Rule", "Implementation Consultant", [
    ("Open Manage Organization, Pay and Job Structures", "", "Config page loads"),
    ("Create a new Workflow Configuration", "WF: WF_Address_Change", "Workflow form opens"),
    ("Assign approver and save", "Approver: Manager", "Workflow saved")])
sc("EC-BUSRULE-301", "Create a Business Rule", "Implementation Consultant", [
    ("Open Configure Business Rules", "", "Rules list loads"),
    ("Create a new onChange rule", "Rule: Default_FTE", "Rule editor opens"),
    ("Define condition and save", "", "Rule saved")])
sc("EC-REPORT-301", "Run Ad Hoc Report", "HR Analyst", [
    ("Open Report Center", "", "Report list loads"),
    ("Open an Employee Central ad hoc report", "Report: Headcount", "Report opens"),
    ("Run with filters and export", "Filter: Active", "Results exported")])
sc("EC-PEOPLE-PROFILE-301", "Edit People Profile Block", "HR Administrator", [
    ("Search and open a People Profile", "Employee: Priya Nair", "Profile opens"),
    ("Edit the Job Information card via the pencil", "", "Edit form opens"),
    ("Change the working time and save", "FTE: 0.8", "Saved")])
sc("EC-JOBREL-301", "Add Matrix Manager", "HR Administrator", [
    ("Open the employee Job Relationships", "Employee: Tom Hardy", "Relationships shown"),
    ("Add a Matrix Manager relationship", "Matrix Mgr: Eva Stone", "Relationship added"),
    ("Save the change", "", "Saved")])
sc("EC-GLOBAL-301", "Add Global Assignment", "HR Administrator", [
    ("Open the employee and Take Action", "Employee: Yuki Sato", "Action menu opens"),
    ("Select Add Global Assignment", "Host: UK", "Assignment form opens"),
    ("Enter dates and submit", "Start: 01/09/2026", "Assignment created")])
sc("EC-CONCURRENT-301", "Add Concurrent Employment", "HR Administrator", [
    ("Open the employee and Take Action", "Employee: Liam Ford", "Action menu opens"),
    ("Select Add Concurrent Employment", "", "Form opens"),
    ("Enter second position and submit", "Position: Trainer", "Concurrent job added")])
sc("EC-CONTRACT-301", "Extend Fixed-Term Contract", "HR Administrator", [
    ("Open the employee Employment Details", "Employee: Noah Reed", "Details open"),
    ("Edit the contract end date", "End: 31/03/2027", "End date updated"),
    ("Save the change", "", "Saved")])
sc("EC-BENEFITS-301", "Enrol in Benefit", "Employee (ESS)", [
    ("Open Benefits then Enrolment", "", "Benefits shown"),
    ("Select a benefit plan", "Plan: Private Medical", "Plan selected"),
    ("Confirm enrolment", "", "Enrolled")])

# ── Edge cases to probe the parser ──────────────────────────────────────────
sc("EC-EDGE-SINGLE-301", "Single Step Scenario", "HR Administrator", [
    ("Open Admin Centre and confirm it loads", "", "Admin Centre opens")])
sc("EC-EDGE-NODATA-301", "No Test Data On Any Step", "HR Administrator", [
    ("Open the employee directory", "", "Directory loads"),
    ("Open the org chart view", "", "Org chart shown"),
    ("Collapse and expand a node", "", "Node toggles")])
sc("EC-EDGE-UNICODE-301", "Unicode & Symbols — £ € → \"quotes\"", "Süpervisor", [
    ("Navigate Admin → Manage Data → Foundation", "Naïve café £100", "Página cargada ✓"),
    ("Enter name with accents: Renée O'Brien", "Renée O'Brien", "Saved — no corruption")])
sc("EC-EDGE-NOEXPECTED-301", "Step With Action But No Expected", "HR Administrator", [
    ("Open the employee record", "Employee: Test User", "Record opens"),
    ("Scroll to the Addresses card", "", "")])  # blank expected: still a step (has action)
sc("EC-EDGE-LONGACTION-301", "Very Long Action Text", "HR Administrator", [
    ("Navigate to the Admin Centre, then open Manage Organization Pay and Job "
     "Structures, locate the Department foundation object for the EMEA region, "
     "open it in edit mode, change the effective-dated name and parent division, "
     "verify the propagation to associated positions, and prepare to save",
     "Dept: EMEA Shared Services", "All sub-steps reachable")])

# Inject raw "noise" rows the parser must skip (section header, PRE-REQ, blanks),
# interleaved with a real scenario afterwards, handled in _write below.
NOISE = [
    ["►", "SECTION", "Termination & Offboarding Suite", "", "", "", "", "", "", ""],
    ["PRE-REQ", "", "Employee must be active before offboarding", "", "", "", "", "", "", ""],
    [None, None, None, None, None, None, None, None, None, None],
]
sc("EC-EDGE-AFTERNOISE-301", "Scenario After Header/PreReq/Blank", "HR Administrator", [
    ("Open the employee to offboard", "Employee: Exit Test", "Record opens"),
    ("Initiate the offboarding checklist", "", "Checklist created")])


def _write():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "EC End-to-End Tests"
    ws.append(HEADER)
    # Title-ish blank to mimic real books
    for sid, name, role, steps in S:
        # Inject noise rows right before the designated after-noise scenario.
        if sid == "EC-EDGE-AFTERNOISE-301":
            for nrow in NOISE:
                ws.append(nrow)
        for i, (action, data, expected) in enumerate(steps, 1):
            ws.append([
                i,                       # Row (within scenario)
                sid if i == 1 else "",   # Script ID only on first row
                name if i == 1 else "",  # Scenario Name only on first row
                role if i == 1 else "",  # Role only on first row
                i,                       # Step number
                action, data, expected,
                "To Be Tested", "",
            ])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUT)
    print(f"Wrote {len(S)} scenarios to {OUT}")


if __name__ == "__main__":
    _write()
