# CLI 플래그 확장 + `help --all` 재구현 design

## Problem

omm의 명령어 옵션/플래그 자유도가 일반적인 CLI 프로그램(git, docker, brew 등) 대비
떨어진다:

- 전역 플래그(공통 `-v`/`-q`/`--json`/`--no-color`)가 없고, `-y`/`--json` 같은 옵션도
  커맨드마다 개별로 선언돼 있어 일관성이 없다.
- 관례적인 짧은 커맨드 별칭(`rm`, `ls`, `up`)이 없다.
- 일부 커맨드(`search`, `uninstall`, `upgrade`, `list`, `link`)에 세부 제어 옵션이
  부족해 스크립팅/자동화 시 우회가 필요하다.

또한 최근 `omm help`를 짧고 큐레이션된 형태로 바꾸면서 `omm help --all`의 역할이
애매해졌다: README처럼 완전한 레퍼런스도 아니고, 짧지도 않다. 코드를 보면 원인이
명확하다 (`cli.py:259-263`):

```python
if all:
    formatter = root_ctx.make_formatter()
    typer.core.TyperGroup.format_help(root_ctx.command, root_ctx, formatter)
    console.print(formatter.getvalue().rstrip("\n"))
    raise typer.Exit(0)
```

이건 Click 기본 `Group.format_help`를 그대로 호출한 것이라 두 가지 구멍이 있다:

1. **중첩 그룹 미전개**: `setting`은 `app.add_typer(setting_app)`으로 붙은 별도
   `click.Group`이다. 최상위 목록엔 "setting" 한 줄(그룹 자체 help)만 나오고, 그 밑의
   `telemetry`/`upload`/`version`/`calibrate`/`catalog-trust`/`catalog-status`/
   `catalog-rollback` 7개 서브커맨드는 전혀 안 보인다.
2. **플래그 미표시**: Click의 `format_commands`는 커맨드 이름 + 한 줄 설명만
   나열한다. 옵션/플래그는 각 커맨드 자신의 `--help`에만 있고, `--all` 리스팅에는
   애초에 포함되지 않는다.

## Scope

포함:
- 전역 플래그 4개 (`--json`, `-y`/`--yes`, `-q`/`--quiet`, `--no-color`),
  커맨드 앞/뒤 양쪽 위치 모두 허용
- 커맨드 별칭 3개 (`rm`, `ls`, `up`)
- 커맨드별 세부 옵션 추가 (아래 표)
- `--json` 지원 범위 확장 (`tune`, `scan`)
- 스크립팅 계약 문서화 + telemetry flush 메시지 stdout 누수 수정
- `omm help --all`을 "모든 커맨드(중첩 포함) + 전체 옵션"을 보여주는 완전판
  레퍼런스로 재구현

제외 (YAGNI):
- `install --dir` 류 설치 경로 커스터마이즈 — `OMM_HOME` 환경변수가 이미 hub
  전체 위치를 오버라이드하므로 불필요. 대신 `help`/README에 `OMM_HOME` 사용법을
  더 잘 보이게 문서화한다.
- `contribute`/`recommend`처럼 대화형/루프형 커맨드의 `--json` 구조화 출력 —
  의미가 안 맞아 제외.
- `-v`/`--verbose` — 코드 확인 결과 지금 omm엔 "숨겨진 상세 로그"가 없다.
  미처리 예외는 이미 일반 traceback으로 흘러가고(`main()`, `cli.py:4056`),
  대신 ~30곳의 `except` 블록이 짧은 에러 메시지만 찍고 원래 traceback을
  버린다. `-v`를 의미 있게 만들려면 그 30곳을 개별적으로 손봐야 해서
  범위가 크고 회귀 리스크도 있다 — 별도 과제로 분리, 이번 플랜에서 제외.
- Typer → 순수 Click 재작성 — 기존 4078줄 파일이 이미 Typer/Click 내부(포매터,
  `TyperGroup`)에 직접 손을 댄 상태라, 전체 재작성은 리스크 대비 이득이 없다.

## Global flags

**방식**: root callback(`_root`)에서 4개 옵션을 eager 파싱해 `ctx.obj`(dataclass
`GlobalOptions`)에 저장한다. 각 서브커맨드에도 동일 옵션을 데코레이터
(`@global_flags`, `cli.py` 상단에 신규 정의)로 자동 주입해 커맨드 뒤 위치도
받는다. 각 함수 시그니처에 반복 선언하는 대신, 데코레이터가 Click 파라미터를
커맨드 객체에 얹고 함수는 `ctx: typer.Context`로 병합된 값을 조회하게 한다.

병합 규칙: 커맨드 뒤에 명시적으로 값이 오면 그 값 우선, 없으면 `ctx.obj`(앞쪽에서
설정된 값), 둘 다 없으면 기본값(`False`/`None`).

| 플래그 | 축약 | 동작 |
|---|---|---|
| `--json` | — | 지원 커맨드에서 구조화 출력. 활성 시 stdout엔 JSON만 나가도록 배너/알림류는 전부 stderr로 |
| `--yes` | `-y` | 확인 프롬프트 전부 생략. 기존 `import`/`uninstall`/`upgrade`/`contribute`에 더해 `install`/`autoremove`에도 적용 |
| `--quiet` | `-q` | 배너·진행 표시줄·안내 메시지 억제, 에러만 stderr로 |
| `--no-color` | — | ANSI 제거. `NO_COLOR` 환경변수도 동일하게 취급 |

`--json`은 기존 4개 커맨드(`search`, `list`, `info`, `benchmark`)에 더해
`tune`, `scan`으로 확장한다 (둘 다 단일 결과를 반환하는 구조라 자연스러움).
`contribute`/`recommend`는 대화형/루프형이라 제외.

## Command aliases

`_RootHelpGroup.get_command()`에 alias dict를 얹어 처리한다 (별도 등록된
`click.Command`가 아니라 이름 조회 시점에 원래 커맨드로 리졸브):

| 별칭 | 원래 커맨드 |
|---|---|
| `rm` | `uninstall` |
| `ls` | `list` |
| `up` | `upgrade` |

`help --all` 목록엔 원래 이름만 노출한다 (별칭은 dict 기반 조회일 뿐 등록된
커맨드가 아니므로 `root_ctx.command.commands` 순회에 자동으로 안 걸림). 각
커맨드 자체 `--help` 마지막 줄에 "alias: rm" 식으로 한 줄 표기한다.

## Per-command additions

| 커맨드 | 추가 플래그 | 비고 |
|---|---|---|
| `install` | `--force` | 이미 설치된 모델도 강제 재다운로드. `--dir`은 제외 (위 Scope 참고) |
| `search` | `--limit N`, `--provider curated\|hf\|modelscope` | 결과 개수 제한, 소스 필터 |
| `uninstall` | `--dry-run` | 뭐가 지워질지만 표시, 실제 삭제 안 함 (`all` 인자와 조합 시 특히 유용) |
| `upgrade` | `--dry-run` | 뭐가 업그레이드될지만 표시 |
| `autoremove` | `--dry-run` | 뭐가 정리될지만 표시 |
| `list` | `--engine NAME` | 특정 엔진에 링크된 것만 표시 |
| `link` | `--engine NAME` | 특정 엔진만 재검증/복구 |

## Scripting contract

- **Exit code**: 이미 일관됨 — `0` 성공, `1` 실패, Click 기본 `2` 사용법 오류.
  변경 없음, `help --all` 출력에 문서화만 추가.
- **stdout/stderr 분리**: 이미 거의 지켜짐 (`err_console` 100곳에서 사용, 빨간
  에러 텍스트가 stdout으로 새는 곳 없음 확인).
- **버그 수정**: `_root` 콜백이 모든 서브커맨드 실행 시 `telemetry.flush_pending()`
  결과를 `console.print`(stdout)로 찍는다 (`cli.py:243-247`). `--json`/`--quiet`
  활성 시 이 라인을 `err_console`로 옮기거나 완전히 억제해 `omm search foo --json | jq`
  같은 파이프라인이 오염되지 않게 한다.

## `help --all` reimplementation

Click 기본 `format_help` 호출을 걷어내고, 이미 단일 커맨드 경로에서 쓰던
`cmd_obj.get_help(sub_ctx)` 패턴을 전체 커맨드에 재사용한다:

1. `root_ctx.command.commands`를 이름순으로 순회.
2. `hidden`인 커맨드(`_bg-version-check`, `relink`)는 스킵.
3. 리프 커맨드면 `cmd.make_context(...)` 후 `cmd.get_help(sub_ctx)` 전문을 출력.
4. 값이 `click.Group`인 경우(`setting`) 그룹 자신의 사용법을 먼저 찍고, 한 단계
   재귀해서 자식 커맨드(`telemetry`, `upload`, `version`, `calibrate`,
   `catalog-trust`, `catalog-status`, `catalog-rollback`) 각각을 3번과 동일하게
   전개한다.
5. 각 커맨드 블록 사이에 구분선(빈 줄 또는 `---`)을 넣어 가독성 확보.

결과적으로 `omm help --all`은 README 요약도 아니고 그렇다고 애매하게 짧지도 않은
"모든 커맨드 + 모든 옵션이 다 나오는 완전판 레퍼런스"가 된다. 새 포매터를 만들
필요 없이 기존 per-command help 인프라(각 `typer.Option`의 `help=` 문자열)를
그대로 재사용하므로 유지보수 부담이 늘지 않는다.

짧은 큐레이션 목록(`omm help`, 인자 없는 `omm --help`)은 지금 그대로 유지한다 —
바뀌는 건 `--all` 경로뿐이다.

## Testing

- 전역 플래그: 앞/뒤/양쪽 위치 각각에 대해 값이 올바르게 병합되는지 단위 테스트.
- 별칭: `omm rm`/`omm ls`/`omm up`이 원래 커맨드와 동일하게 동작하는지, `help --all`
  목록엔 안 나오는지 테스트.
- `help --all`: 출력에 `setting telemetry` 등 중첩 서브커맨드 이름과 그 옵션
  문자열이 포함되는지, hidden 커맨드는 빠지는지 테스트.
- telemetry flush 메시지: `--json` 활성 상태에서 stdout이 유효한 JSON만
  포함하는지 (pending telemetry가 있는 상태를 모킹해서) 테스트.

## Docs

- README/`help` 텍스트에 `OMM_HOME` 환경변수 사용법 문서화 (모델 저장 위치를
  바꾸고 싶을 때의 정식 경로임을 명시).
