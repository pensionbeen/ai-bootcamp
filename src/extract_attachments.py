
from project_paths import PROJECT_ROOT
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin


def extract_attachments(root, metadata):
    xml_seq = root.findtext("./자치법규기본정보/자치법규일련번호")

    # 본문과 첨부자료가 같은 조례 버전에 속하는지 확인합니다.
    if xml_seq != metadata["ordin_seq"]:
        raise ValueError("XML과 metadata의 조례 일련번호가 다릅니다.")

    attachments = []

    for node in root.findall("./별표단위"):
        attachment_key = node.get("별표키")

        if not attachment_key:
            raise ValueError("별표키가 없는 첨부자료가 있습니다.")

        kind = node.findtext("별표구분", "").strip()
        number = node.findtext("별표번호", "").strip()
        branch = node.findtext("별표가지번호", "00").strip()

        # 예: '0001' → '별표 1'
        label = f"{kind} {int(number)}"
        if int(branch) != 0:
            label += f"의{int(branch)}"

        # 이 XML에서는 '별표첨부파일명'에 다운로드 주소가 들어 있습니다.
        raw_url = node.findtext("별표첨부파일명", "").strip()

        attachments.append({
            "attachment_id": f"{metadata['ordin_seq']}:{attachment_key}",
            "attachment_key": attachment_key,
            "attachment_type": kind,
            "attachment_no": number,
            "attachment_branch_no": branch,
            "label": label,
            "title": node.findtext("별표제목", "").strip(),
            "file_type": node.findtext(
                "별표첨부파일구분", ""
            ).strip().lower(),
            "download_url": (
                urljoin("https://www.law.go.kr/", raw_url)
                if raw_url else None
            ),
            "raw_text": node.findtext("별표내용", ""),
        })

    return attachments


def main():
    base_dir = PROJECT_ROOT
    raw_path = base_dir / "data" / "raw" / "2171833.xml"

    root = ET.parse(raw_path).getroot()
    metadata = json.loads(
        raw_path.with_suffix(".metadata.json").read_text(encoding="utf-8")
    )

    attachments = extract_attachments(root, metadata)

    document = {
        "ordin_name": root.findtext("./자치법규기본정보/자치법규명"),
        "metadata": metadata,
        "attachments": attachments,
    }

    output_path = (
        base_dir / "data" / "processed"
        / f"{metadata['ordin_seq']}.attachments.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("첨부자료 개수:", len(attachments))
    print("저장 위치:", output_path)

    for item in attachments:
        link_status = "있음" if item["download_url"] else "없음"
        print(
            f"{item['label']} | {item['file_type']} | "
            f"링크 {link_status} | {item['title']}"
        )


if __name__ == "__main__":
    main()