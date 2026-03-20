import json
import re

import toml
from ruamel.yaml import YAML


def get_project_version():
    """Reads the version from pyproject.toml."""
    with open("pyproject.toml", "r") as f:
        data = toml.load(f)
        return data["project"]["version"]


def update_readme(version):
    """Updates the version in README.md."""
    with open("README.md", "r+") as f:
        content = f.read()
        # Update "Community Edition vX.Y.Z" badge
        new_content = re.sub(r"(Community Edition v)\d+\.\d+\.\d+", rf"\g<1>{version}", content)
        # Update "Features (Version X.Y.Z)" heading
        new_content = re.sub(r"(Features \(Version )\d+\.\d+\.\d+\)", rf"\g<1>{version})", new_content)
        # Update Docker image tags in code blocks
        new_content = re.sub(r"(greenkube/greenkube:)\d+\.\d+\.\d+", rf"\g<1>{version}", new_content)
        # Update version in API health response examples
        new_content = re.sub(r'("version":")\d+\.\d+\.\d+"', rf'\g<1>{version}"', new_content)
        f.seek(0)
        f.write(new_content)
        f.truncate()


def update_ci_cd(version):
    """Updates the version in .github/workflows/ci-cd.yml."""
    with open(".github/workflows/ci-cd.yml", "r+") as f:
        content = f.read()
        new_content = re.sub(r"(tags: greenkube/greenkube:)\d+\.\d+\.\d+", rf"\g<1>{version}", content)
        f.seek(0)
        f.write(new_content)
        f.truncate()


def update_helm_chart_yaml(version):
    """Updates the version in helm-chart/Chart.yaml."""
    yaml = YAML()
    with open("helm-chart/Chart.yaml", "r") as f:
        chart = yaml.load(f)

    chart["version"] = version
    chart["appVersion"] = version

    with open("helm-chart/Chart.yaml", "w") as f:
        yaml.dump(chart, f)


def update_helm_values_yaml(version):
    """Updates the version in helm-chart/values.yaml."""
    yaml = YAML()
    with open("helm-chart/values.yaml", "r") as f:
        values = yaml.load(f)

    values["image"]["tag"] = version

    with open("helm-chart/values.yaml", "w") as f:
        yaml.dump(values, f)


def update_init_py(version):
    """Updates the version in src/greenkube/__init__.py."""
    with open("src/greenkube/__init__.py", "w") as f:
        f.write(f'__version__ = "{version}"\n')


def update_demo_data_generator(version):
    """Updates the version in src/greenkube/demo/data_generator.py."""
    with open("src/greenkube/demo/data_generator.py", "r+") as f:
        content = f.read()
        new_content = re.sub(
            r'(calculation_version=")[\d]+\.[\d]+\.[\d]+(-demo")',
            rf"\g<1>{version}\g<2>",
            content,
        )
        f.seek(0)
        f.write(new_content)
        f.truncate()


def update_frontend_package_json(version):
    """Updates the version in frontend/package.json."""
    with open("frontend/package.json", "r") as f:
        data = json.load(f)

    data["version"] = version

    with open("frontend/package.json", "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def main():
    version = get_project_version()
    print(f"Syncing version: {version}")

    update_readme(version)
    print("Updated README.md")

    update_ci_cd(version)
    print("Updated .github/workflows/ci-cd.yml")

    update_helm_chart_yaml(version)
    print("Updated helm-chart/Chart.yaml")

    update_helm_values_yaml(version)
    print("Updated helm-chart/values.yaml")

    update_init_py(version)
    print("Updated src/greenkube/__init__.py")

    update_demo_data_generator(version)
    print("Updated src/greenkube/demo/data_generator.py")

    update_frontend_package_json(version)
    print("Updated frontend/package.json")

    print("Version sync complete.")


if __name__ == "__main__":
    main()
