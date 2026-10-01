# CI 워크플로 켜기

이 폴더의 `ci.yml` 는 2026-10-01 점검 때 만든 GitHub Actions 워크플로 개정안입니다(지금 켜져 있는 .github/workflows/ci.yml 을 대체). 반영에 쓴 gh 로그인 토큰에 워크플로 파일을 올리는 권한(workflow scope)이 없어 GitHub 이 푸시를 거부했기 때문에, 아직 켜지지 않은 위치에 두었습니다.

켜려면 저장소 폴더에서 아래를 실행합니다.

```
gh auth refresh -h github.com -s workflow
git mv -f .github/ci/ci.yml .github/workflows/ci.yml
git commit -m "Enable CI workflow" && git push
```
