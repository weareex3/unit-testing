"""Generate 5 EC sample scripts (5 steps each) using REAL tenant users, so live
runs actually find their targets. Parser-format workbook (sheet name has
'End-to-End'; name/role only on a scenario's first row).

Run:  python -m eval.gen_ec_sample5
Writes: scripts/EX3_EC_Sample5_V1.xlsx
"""
from pathlib import Path
import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "scripts" / "EX3_EC_Sample5_V1.xlsx"
HEADER = ["Row", "Script ID", "Scenario Name", "Role", "Step", "Action",
          "Test Data", "Expected Result", "Status", "Comments"]

# Real users in the preview tenant
ESTHER = "Esther Loh"
ALEX = "Alex Brackley"
ME = "Louie Bond"

S = []
def sc(sid, name, role, steps): S.append((sid, name, role, steps))

# 1) View Esther Loh's profile — read-only, 5 steps
sc("EC-SAMP-401", "View Employee Profile", "HR Administrator", [
    ("Use the global search to find the employee", f"Employee: {ESTHER}", "Search results appear"),
    ("Open the person's People Profile from the results", f"{ESTHER}", "People Profile opens"),
    ("Open the Job Information card", "", "Job Information is displayed"),
    ("Scroll to the Addresses card", "", "Addresses card is visible"),
    ("Open the Compensation Information block", "", "Compensation is displayed")])

# 2) Edit Alex Brackley's address — stops before save (preview), 5 steps
sc("EC-SAMP-402", "Edit Employee Address", "HR Administrator", [
    ("Search for the employee", f"Employee: {ALEX}", "Search results appear"),
    ("Open the People Profile", f"{ALEX}", "Profile opens"),
    ("Scroll to the Addresses card and click its Edit pencil", "", "Address edit form opens"),
    ("Update the postcode field", "Postcode: SW1A 1AA", "Postcode field updated"),
    ("Review the change ready for saving", "", "Form shows the new value")])

# 3) Update my own personal info — ESS, stops before save, 5 steps
sc("EC-SAMP-403", "Update Personal Information", "Employee (ESS)", [
    ("Open My Profile from the header avatar", "", "My Profile opens"),
    ("Open the Personal Information block", f"{ME}", "Personal Information displayed"),
    ("Click the Edit pencil on Personal Information", "", "Edit form opens"),
    ("Update the marital status field", "Status: Married", "Marital status updated"),
    ("Review the change ready for saving", "", "Form shows the new value")])

# 4) Proxy as Esther Loh — 5 steps
sc("EC-SAMP-404", "Proxy as Another User", "HR Administrator", [
    ("Click the avatar in the top-right header", "", "User menu opens"),
    ("Choose Proxy Now from the menu", "", "Proxy dialog opens"),
    ("Type the target employee's name", f"{ESTHER}", "Matching person appears"),
    ("Select the matching person from the dropdown", f"{ESTHER}", "Person selected"),
    ("Confirm to start the proxy session", "", "Proxy session active")])

# 5) Review Alex Brackley's job information — read-only, 5 steps
sc("EC-SAMP-405", "Review Job Information", "Line Manager", [
    ("Search for the employee", f"Employee: {ALEX}", "Search results appear"),
    ("Open the People Profile", f"{ALEX}", "Profile opens"),
    ("Open the Job Information card", "", "Job Information displayed"),
    ("Check the job title and department", "", "Title and department visible"),
    ("Check the reporting manager", "", "Manager is shown")])


def _write():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "EC End-to-End Tests"
    ws.append(HEADER)
    for sid, name, role, steps in S:
        for i, (action, data, expected) in enumerate(steps, 1):
            ws.append([i, sid if i == 1 else "", name if i == 1 else "",
                       role if i == 1 else "", i, action, data, expected, "To Be Tested", ""])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUT)
    print(f"Wrote {len(S)} scripts x 5 steps to {OUT}")


if __name__ == "__main__":
    _write()
