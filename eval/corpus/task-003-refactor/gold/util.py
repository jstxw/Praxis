"""Helpers for filtering and normalizing user records."""


def _process_role(users, role):
    result = []
    for user in users:
        if not user.get("active"):
            continue
        if user.get("role") != role:
            continue
        result.append(
            {
                "id": user["id"],
                "name": user["name"].strip().title(),
                "email": user["email"].lower(),
            }
        )
    return result


def process_admins(users):
    return _process_role(users, "admin")


def process_managers(users):
    return _process_role(users, "manager")


def process_engineers(users):
    return _process_role(users, "engineer")
