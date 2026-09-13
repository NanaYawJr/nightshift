"""Week 1 checkpoint: prove the service principal can read the tenant.

Run:  python -m collectors._smoke
Expect a list of workspaces the principal has been granted access to.
An empty list means auth works but no workspace role has been assigned yet.
"""

from common.fabric_api import get_paged


def main() -> None:
    workspaces = list(get_paged("/workspaces"))

    if not workspaces:
        print("Authenticated, but the principal can see no workspaces.")
        print("Add it as Viewer on a workspace, then run again.")
        return

    print(f"{len(workspaces)} workspace(s) visible:\n")
    for ws in workspaces:
        print(f"  {ws.get('displayName'):<30} {ws.get('id')}")


if __name__ == "__main__":
    main()
