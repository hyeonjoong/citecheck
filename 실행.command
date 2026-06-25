#!/bin/bash
# 더블클릭하면 citecheck가 어떻게 동작하는지 바로 보여줍니다.
cd "$(dirname "$0")"

echo "=================================================="
echo "  citecheck — 원고 인용/DOI를 Crossref로 검증"
echo "  깨진 DOI · 메타데이터 불일치 · 철회 논문 탐지"
echo "=================================================="
echo
echo "[사용법]"
echo "  citecheck <참고문헌파일>          # .bib 또는 텍스트"
echo "  citecheck refs.bib --json         # 기계용 JSON 출력"
echo "  citecheck refs.bib --verbose      # 정상 항목까지 표시"
echo
echo "--------------------------------------------------"
echo "[예시] 동봉된 examples/sample.bib 로 지금 실행해봅니다:"
echo "--------------------------------------------------"
echo

# 설치돼 있으면 citecheck 명령, 아니면 모듈로 폴백
if command -v citecheck >/dev/null 2>&1; then
  citecheck examples/sample.bib --verbose --no-color
else
  python3 -m citecheck examples/sample.bib --verbose --no-color
fi

echo
echo "--------------------------------------------------"
echo "[내 논문에 쓰려면]"
echo "  위 'citecheck' 자리에 본인 .bib 경로를 넣으세요. 예:"
echo "  citecheck ~/Downloads/02_프로젝트/논문_투고/BELL_Paper3_Sleep_7th_Domain/references.bib"
echo "--------------------------------------------------"
echo
read -p "엔터를 누르면 창이 닫힙니다..."
