# 파일럿 수정 결과

동일 thread resume exit0. 두 번째 턴 사용량은 누적값 차분으로 input3,967,027/output28,308, 합계3,995,335. 두 턴 누적6,198,289 tokens이며 CLI값과 응답별 session audit이 일치한다.

실제 코드 변경: KST 날짜 경계를 application에서 Instant로 계산하여 PostgreSQL timestamptz 파라미터로 전달, application→api DTO 역의존 제거, 금액/멱등 로직 모듈화, fake bank 및 경계/오류 테스트 추가. 모델은 단위/통합11개와 restart script 통과를 보고했다. 외부verify2에서 C01~C12,C15,C16,C18의 HTTP+DB+은행 대조 통과를 확인했다. 장애/KST 후속 증거는 verification-02 폴더 참조. 부하와 최종 본 실험 완료를 의미하지 않는다.

문제: Kotlin/Mockito matcher 사용 중 테스트 NPE가 발생했고 matcher 수정 뒤 모델 테스트가 통과했다. 자체 기록 REVIEW_FIXES.md를 보존한다. Fable은 최초코드에1회 실행했으며 수정본을 재리뷰한 것으로 표시하지 않는다.
