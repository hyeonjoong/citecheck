#!/bin/bash
# 더블클릭하면 citecheck가 어떻게 동작하는지 바로 보여줍니다.
cd "$(dirname "$0")"

echo "=================================================="
echo "  citecheck - 원고 인용/DOI를 Crossref로 검증"
echo "  깨진 DOI, 메타데이터 불일치, 철회/우려표명, 정정"
echo ", 정식 출판된 프리프린트, 중복 DOI 탐지"
echo "=================================================="
echo
echo "  ※ 인터넷 연결이 필요합니다 (Crossref/doi.org 조회)."
echo
echo "[사용법]"
echo "  python3 -m citecheck <참고문헌파일>          # .bib/.ris/.json/.csv/.xlsx/.docx/텍스트 (자동 인식)"
echo "  python3 -m citecheck refs.bib --json         # 기계용 JSON 출력"
echo "  python3 -m citecheck refs.bib --report csv   # 엑셀로 열 CSV (공동저자 공유)"
echo "  python3 -m citecheck refs.bib --pubmed       # PubMed로 철회/PMIDvsDOI 교차검증"
echo "  python3 -m citecheck refs.bib --suggest-doi  # DOI 없는 항목의 DOI를 찾아서 제안"
echo "  python3 -m citecheck refs.bib --cache        # 조회 캐시 (재실행이 즉시 끝남)"
echo "  python3 -m citecheck refs.bib --profile      # 참고문헌 목록 통계 (중앙 나이, Price 지수, 저널 분포)"
echo "  python3 -m citecheck included_studies.xlsx   # 엑셀 문헌표를 변환 없이 그대로 검사"
echo "  python3 -m citecheck refs.bib --strict --ignore no-doi  # 제출 전 게이트"
echo "  python3 -m citecheck --list-checks          # 끌 수 있는 검사 코드 보기"
echo "  python3 -m citecheck refs.bib --verbose    # 정상 항목까지 표시"
echo "  python3 -m citecheck refs.bib --strict     # 경고도 실패로 처리"
echo
echo "--------------------------------------------------"
echo "[예시] 동봉된 examples/sample.bib 로 지금 실행해봅니다:"
echo "  (Crossref 조회로 몇 초 걸릴 수 있습니다)"
echo "--------------------------------------------------"
echo

# 이 폴더의 citecheck를 실행합니다. 따로 설치된 citecheck 명령(예전 버전일 수
# 있음)이 대신 실행되지 않도록 항상 모듈로 부릅니다.
python3 -m citecheck examples/sample.bib --verbose --no-color --profile

echo
echo "--------------------------------------------------"
echo "[내 논문에 쓰려면]"
echo "  위 명령의 examples/sample.bib 자리에 본인 .bib 경로를 넣으세요. 예:"
echo "  python3 -m citecheck ~/path/to/references.bib"
echo "--------------------------------------------------"
echo
read -p "엔터를 누르면 창이 닫힙니다..."
