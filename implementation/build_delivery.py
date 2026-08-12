from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml


REQUIRED = {
    "README.txt",
    "change_request.txt",
    "migration_plan.csv",
    "field_map.csv",
    "legacy_values/preview.yaml",
    "legacy_values/production.yaml",
    "starter/gateway-chart/Chart.yaml",
    "starter/gateway-chart/values.yaml",
    "starter/gateway-chart/templates/deployment.yaml",
    "starter/gateway-chart/templates/configmap.yaml",
    "starter/gateway-chart/templates/service.yaml",
}


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, encoding="utf-8", errors="strict", capture_output=True, timeout=180)


def nested_get(data: dict, dotted: str):
    current = data
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"旧值缺少字段:{dotted}")
        current = current[part]
    return current


def nested_set(data: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    current = data
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def object_key(item: dict) -> tuple[str, str, str]:
    meta = item.get("metadata", {})
    return item.get("kind", ""), meta.get("namespace", ""), meta.get("name", "")


def docs(text: str) -> list[dict]:
    result = [item for item in yaml.safe_load_all(text) if item]
    keys = [object_key(item) for item in result]
    if not result or len(keys) != len(set(keys)):
        raise ValueError("候选清单对象为空或身份重复")
    return result


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--helm", required=True)
    args = parser.parse_args()
    source = Path(args.input).resolve()
    output = Path(args.output).resolve()
    if output.exists():
        shutil.rmtree(output)
    present = {item.relative_to(source).as_posix() for item in source.rglob("*") if item.is_file()}
    if not REQUIRED.issubset(present):
        raise ValueError("版本组材料不完整")

    with (source / "migration_plan.csv").open(encoding="utf-8", newline="") as handle:
        plan = list(csv.DictReader(handle))
    with (source / "field_map.csv").open(encoding="utf-8", newline="") as handle:
        mapping = list(csv.DictReader(handle))
    if {row["environment"] for row in plan} != {"preview", "production"} or not mapping:
        raise ValueError("环境计划或字段映射不完整")
    expected_targets = {
        "image.repository", "image.tag", "replicaCount", "service.port", "gateway.routeMode",
        "gateway.upstreamTimeoutSeconds", "gateway.requestIdHeader", "gateway.noticeBanner",
    }
    if {row["target_path"] for row in mapping} != expected_targets:
        raise ValueError("字段映射没有覆盖网关发布入口")

    temp = Path(tempfile.mkdtemp(prefix="gateway-chart-", dir=output.parent))
    try:
        chart = temp / "chart/edge-gateway"
        values_dir = temp / "values"
        renders = temp / "renders"
        reports = temp / "reports"
        shutil.copytree(source / "starter/gateway-chart", chart)
        values_dir.mkdir(parents=True)
        renders.mkdir()
        reports.mkdir()

        (chart / "values.yaml").write_text(
            "image:\n  repository: registry.example.invalid/edge/gateway\n  tag: 2.6.1\n"
            "replicaCount: 1\nservice:\n  port: 8080\ngateway:\n  routeMode: stable\n"
            "  upstreamTimeoutSeconds: 5\n  requestIdHeader: X-Edge-Request\n  noticeBanner: |\n"
            "    边缘网关运行提示\n",
            encoding="utf-8",
        )
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["image", "replicaCount", "service", "gateway"],
            "properties": {
                "image": {"type": "object", "additionalProperties": False, "required": ["repository", "tag"], "properties": {"repository": {"type": "string", "minLength": 1}, "tag": {"type": "string", "minLength": 1}}},
                "replicaCount": {"type": "integer", "minimum": 1},
                "service": {"type": "object", "additionalProperties": False, "required": ["port"], "properties": {"port": {"type": "integer", "minimum": 1, "maximum": 65535}}},
                "gateway": {"type": "object", "additionalProperties": False, "required": ["routeMode", "upstreamTimeoutSeconds", "requestIdHeader", "noticeBanner"], "properties": {"routeMode": {"type": "string", "enum": ["canary", "stable"]}, "upstreamTimeoutSeconds": {"type": "integer", "minimum": 1}, "requestIdHeader": {"type": "string", "minLength": 1}, "noticeBanner": {"type": "string", "minLength": 1}}},
            },
        }
        (chart / "values.schema.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (chart / "templates/deployment.yaml").write_text(
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{ .Release.Name }}\n"
            "spec:\n  replicas: {{ .Values.replicaCount }}\n  selector:\n    matchLabels:\n"
            "      app.kubernetes.io/name: {{ .Release.Name }}\n  template:\n    metadata:\n      labels:\n"
            "        app.kubernetes.io/name: {{ .Release.Name }}\n    spec:\n      containers:\n"
            "        - name: gateway\n          image: {{ printf \"%s:%s\" .Values.image.repository .Values.image.tag | quote }}\n"
            "          ports:\n            - name: http\n              containerPort: {{ .Values.service.port }}\n"
            "          envFrom:\n            - configMapRef:\n                name: {{ .Release.Name }}-runtime\n",
            encoding="utf-8",
        )
        (chart / "templates/service.yaml").write_text(
            "apiVersion: v1\nkind: Service\nmetadata:\n  name: {{ .Release.Name }}\nspec:\n  selector:\n"
            "    app.kubernetes.io/name: {{ .Release.Name }}\n  ports:\n    - name: http\n"
            "      port: {{ .Values.service.port }}\n      targetPort: http\n",
            encoding="utf-8",
        )
        (chart / "templates/configmap.yaml").write_text(
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: {{ .Release.Name }}-runtime\ndata:\n"
            "  routeMode: {{ .Values.gateway.routeMode | quote }}\n"
            "  upstreamTimeoutSeconds: {{ .Values.gateway.upstreamTimeoutSeconds | quote }}\n"
            "  requestIdHeader: {{ .Values.gateway.requestIdHeader | quote }}\n"
            "  noticeBanner: |\n{{ .Values.gateway.noticeBanner | indent 4 }}\n",
            encoding="utf-8",
        )

        lint = run([args.helm, "lint", str(chart)])
        if lint.returncode:
            raise RuntimeError(lint.stdout + lint.stderr)

        migration_rows: list[dict] = []
        review_rows: list[dict] = []
        for row in plan:
            legacy_path = source / row["values_file"]
            legacy = yaml.safe_load(legacy_path.read_text(encoding="utf-8"))
            target: dict = {}
            for item in mapping:
                value = nested_get(legacy, item["legacy_path"])
                nested_set(target, item["target_path"], value)
                migration_rows.append({
                    "environment": row["environment"], "legacy_path": item["legacy_path"],
                    "target_path": item["target_path"], "consumer": item["consumer"], "status": "MAPPED",
                })
            values_path = values_dir / f"{row['environment']}.yaml"
            values_path.write_text(yaml.safe_dump(target, allow_unicode=True, sort_keys=False), encoding="utf-8")
            rendered = run([args.helm, "template", row["release_name"], str(chart), "--namespace", row["namespace"], "--values", str(values_path)])
            if rendered.returncode:
                raise RuntimeError(rendered.stdout + rendered.stderr)
            objects = {item["kind"]: item for item in docs(rendered.stdout)}
            if set(objects) != {"Deployment", "Service", "ConfigMap"}:
                raise ValueError("候选清单对象集合不符合版本组用途")
            deployment = objects["Deployment"]
            service = objects["Service"]
            config = objects["ConfigMap"]
            container = deployment["spec"]["template"]["spec"]["containers"][0]
            expected_image = f"{target['image']['repository']}:{target['image']['tag']}"
            checks = {
                "image": container["image"] == expected_image,
                "replicaCount": deployment["spec"]["replicas"] == target["replicaCount"],
                "service.port": service["spec"]["ports"][0]["port"] == target["service"]["port"],
                "gateway.routeMode": config["data"]["routeMode"] == target["gateway"]["routeMode"],
                "gateway.upstreamTimeoutSeconds": config["data"]["upstreamTimeoutSeconds"] == str(target["gateway"]["upstreamTimeoutSeconds"]),
                "gateway.requestIdHeader": config["data"]["requestIdHeader"] == target["gateway"]["requestIdHeader"],
                "gateway.noticeBanner": config["data"]["noticeBanner"] == target["gateway"]["noticeBanner"],
                "service.selector": service["spec"]["selector"] == deployment["spec"]["selector"]["matchLabels"],
            }
            if not all(checks.values()):
                raise ValueError(f"环境迁移结果不完整:{row['environment']}")
            for field, passed in checks.items():
                review_rows.append({"environment": row["environment"], "check": field, "status": "PASS" if passed else "FAIL", "evidence": f"renders/{row['environment']}.yaml"})
            (renders / f"{row['environment']}.yaml").write_text(rendered.stdout, encoding="utf-8")

        write_csv(reports / "field_migration.csv", migration_rows, ["environment", "legacy_path", "target_path", "consumer", "status"])
        write_csv(reports / "release_review.csv", review_rows, ["environment", "check", "status", "evidence"])
        (temp / "release_note.md").write_text(
            "# 边缘网关Chart配置合并\n\n"
            "preview和production已经按migration_plan.csv整理成独立values，并由同一套Chart生成候选清单。\n\n"
            "版本负责人可从field_migration.csv回查旧字段去向，再用release_review.csv定位清单中的消费位置。现场安装、流量切换和运行观察由变更窗口值班处理。\n",
            encoding="utf-8",
        )
        temp.rename(output)
    except Exception:
        if temp.exists():
            shutil.rmtree(temp)
        if output.exists():
            shutil.rmtree(output)
        raise


if __name__ == "__main__":
    main()
