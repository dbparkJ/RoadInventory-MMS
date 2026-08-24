import {
  BoxSelect,
  CheckCircle2,
  ClipboardCheck,
  Download,
  ListChecks,
  ShieldCheck,
  UtilityPole,
} from 'lucide-react'
import { useState, type ReactNode } from 'react'
import './OperatorWorkflowGuide.css'

type GuideSection = 'review' | 'objects' | 'finish'

const GUIDE_SECTIONS: ReadonlyArray<{
  id: GuideSection
  label: string
  description: string
}> = [
  {
    id: 'review',
    label: '1. 검수 작업',
    description: '범위와 후보를 만들고 하나씩 판정합니다.',
  },
  {
    id: 'objects',
    label: '2. 결과 확인·수정',
    description: '검출 결과의 상세 속성을 확인하고 바로잡습니다.',
  },
  {
    id: 'finish',
    label: '3. QA·완료',
    description: '오류를 해소하고 결과를 내보냅니다.',
  },
]

const OPERATOR_GUIDE_PANEL_ID = 'operator-guide-panel'

export function OperatorWorkflowGuide() {
  const [section, setSection] = useState<GuideSection>('review')
  const current = GUIDE_SECTIONS.find((item) => item.id === section) ?? GUIDE_SECTIONS[0]

  return (
    <section className="operator-workflow-guide" aria-labelledby="operator-workflow-title">
      <header>
        <span className="operator-workflow-icon"><ClipboardCheck size={18} /></span>
        <div>
          <strong id="operator-workflow-title">처음 사용하는 작업자라면</strong>
          <p>검수 작업은 AI를 다시 실행하는 기능이 아니라, 확인할 범위와 판단 기록을 묶어 관리하는 작업 묶음입니다.</p>
        </div>
      </header>

      <div className="operator-workflow-tabs" role="group" aria-label="운영자 작업 안내">
        {GUIDE_SECTIONS.map((item) => (
          <button
            key={item.id}
            id={`operator-guide-tab-${item.id}`}
            type="button"
            aria-pressed={section === item.id}
            aria-controls={OPERATOR_GUIDE_PANEL_ID}
            onClick={() => setSection(item.id)}
          >
            <strong>{item.label}</strong>
            <span>{item.description}</span>
          </button>
        ))}
      </div>

      <div
        id={OPERATOR_GUIDE_PANEL_ID}
        className="operator-workflow-panel"
        role="region"
        aria-labelledby={`operator-guide-tab-${section}`}
      >
        <span className="operator-workflow-current">{current.label}</span>
        {section === 'review' ? (
          <ReviewWorkGuide />
        ) : section === 'objects' ? (
          <ObjectWorkGuide />
        ) : (
          <FinishWorkGuide />
        )}
      </div>
    </section>
  )
}

function ReviewWorkGuide() {
  return (
    <>
      <p className="operator-workflow-definition">
        한 작업 묶음에는 <strong>완료된 검출 run</strong>, <strong>수정할 Point 레이어</strong>,
        <strong> 트랙·프레임 범위</strong>, 후보별 판정 기록이 함께 관리됩니다.
      </p>
      <ol className="operator-workflow-steps">
        <GuideStep number="1" title="범위 준비">
          데이터셋에서 프레임 또는 범위를 고릅니다. 완료된 검출 run과 저장할 Point 레이어가 있어야 합니다.
        </GuideStep>
        <GuideStep number="2" title="검수 작업 시작">
          상단의 <strong>새 검수 작업</strong>에서 run·레이어·범위와 후보 source를 선택한 뒤
          <strong> 검수 작업 시작</strong>을 누릅니다.
        </GuideStep>
        <GuideStep number="3" title="후보 확인">
          후보는 저장된 객체가 아니라 <strong>확인할 일감</strong>입니다. <strong>항목 목록</strong>을 열고
          항목을 선택하면 해당 프레임으로 이동합니다.
        </GuideStep>
        <GuideStep number="4" title="하나씩 판정">
          정상은 <strong>완료</strong>, 잘못 검출한 것은 <strong>오검출</strong>, 판단이 어려우면
          <strong> 현장조사</strong> 또는 <strong>건너뛰기</strong>로 기록합니다.
        </GuideStep>
        <GuideStep number="5" title="누락·위치 오류 처리">
          파노라마나 3D 화면에서 결과를 선택한 뒤 <strong>자세히</strong> 또는
          <strong> 수정하기</strong>를 사용합니다. 속성표에서 저장한 변경은 해당 결과와 함께 반영됩니다.
        </GuideStep>
      </ol>
      <Callout icon={<ListChecks size={15} />} title="진행 상태는 자동 보존됩니다">
        다른 프레임으로 이동하거나 창을 다시 열어도 선택한 작업과 필터를 이어서 사용할 수 있습니다.
      </Callout>
    </>
  )
}

function ObjectWorkGuide() {
  return (
    <>
      <p className="operator-workflow-definition">
        파노라마나 3D 화면에서 검출 결과를 선택하면 <strong>자세히</strong>와
        <strong> 수정하기</strong>가 표시됩니다. 상세 내용을 먼저 확인하고 필요한 경우 속성표에서 수정하세요.
      </p>
      <div className="operator-object-guides">
        <article aria-labelledby="traffic-sign-guide-title">
          <header>
            <BoxSelect size={16} />
            <div>
              <strong id="traffic-sign-guide-title">교통표지판 · 파노라마 bbox</strong>
              <small>검출 박스와 속성을 함께 확인합니다</small>
            </div>
          </header>
          <ol>
            <li>파노라마에서 AI 검출 박스를 클릭합니다.</li>
            <li><strong>자세히</strong>를 눌러 클래스, 신뢰도와 나머지 속성을 확인합니다.</li>
            <li><strong>수정하기</strong>를 누르면 연결된 피처가 속성표 편집기로 열립니다.</li>
            <li>값을 고쳐 저장하거나 피처를 삭제합니다. 삭제한 피처와 연결된 파노라마 박스도 함께 사라집니다.</li>
          </ol>
          <p>레이어에 연결되지 않은 원본 AI 결과는 상세 확인만 가능하며 수정 버튼은 비활성화됩니다.</p>
        </article>

        <article aria-labelledby="support-pole-guide-title">
          <header>
            <UtilityPole size={16} />
            <div>
              <strong id="support-pole-guide-title">표지 지주 · 점군 B → B</strong>
              <small>선택 포인트와 연결 관계를 집중해서 봅니다</small>
            </div>
          </header>
          <ol>
            <li>3D 화면이나 속성표에서 표지·신호등 또는 지주를 선택합니다.</li>
            <li>선택 대상 주변은 조밀한 포인트로, 나머지 영역은 성긴 포인트로 전환됩니다.</li>
            <li>촬영 위치와 선택 객체, 연결된 지주 사이의 안내선을 확인합니다.</li>
            <li>속성을 바꿔야 하면 선택 카드의 <strong>수정하기</strong>로 편집기를 엽니다.</li>
          </ol>
          <p>정확한 연결 피처를 불러오는 동안에도 선택 대상은 즉시 강조되고, 준비가 끝나면 연결 지주가 함께 표시됩니다.</p>
        </article>
      </div>
      <Callout icon={<CheckCircle2 size={15} />} title="저장 전 공통 확인">
        선택한 대상과 레이어가 맞는지 확인하세요. 삭제는 파노라마 검출 박스에도 즉시 반영됩니다.
      </Callout>
    </>
  )
}

function FinishWorkGuide() {
  return (
    <>
      <p className="operator-workflow-definition">
        작업 큐의 항목을 모두 처리한 뒤 QA를 실행합니다. QA는 현재 레이어 내용과 검수 기록이 서로 맞는지
        마지막으로 검사하는 단계입니다.
      </p>
      <ol className="operator-workflow-steps">
        <GuideStep number="1" title="미처리 항목 확인">
          대기·검수 중 항목이 남아 있지 않은지 확인합니다. 생성된 후보가 0개라면 바로 QA로 진행할 수 있습니다.
        </GuideStep>
        <GuideStep number="2" title="QA 실행">
          <kbd>Q</kbd> 또는 <strong>QA</strong>를 열고 <strong>QA 검사 실행</strong>을 누릅니다.
        </GuideStep>
        <GuideStep number="3" title="오류 해소">
          오류는 해당 데이터를 수정하고 QA를 다시 실행해야 없어집니다. 경고는 해결하거나 3자 이상의 사유로 무시할 수 있습니다.
        </GuideStep>
        <GuideStep number="4" title="검수 작업 완료">
          완료 가능 상태가 되면 <strong>검수 작업 완료</strong>를 누릅니다. 차단 이유가 보이면 안내된 항목부터 처리합니다.
        </GuideStep>
        <GuideStep number="5" title="결과 받기">
          보고서는 처리 통계와 판정 기록을, 편집 결과 ZIP은 저장된 피처와 메타데이터를 담습니다. Active-learning ZIP은
          서버에서 허용된 경우에만 표시됩니다.
        </GuideStep>
      </ol>
      <div className="operator-finish-notes">
        <Callout icon={<ShieldCheck size={15} />} title="QA를 다시 실행해야 하는 경우">
          QA 이후 피처를 추가·수정했다면 이전 결과는 오래된 상태가 됩니다. 최신 데이터로 QA를 다시 실행하세요.
        </Callout>
        <Callout icon={<Download size={15} />} title="Active-learning 내보내기">
          학습용 자료만 생성합니다. 자동 학습이나 모델 배포는 실행하지 않습니다.
        </Callout>
      </div>
    </>
  )
}

function GuideStep({
  number,
  title,
  children,
}: {
  number: string
  title: string
  children: ReactNode
}) {
  return (
    <li>
      <span>{number}</span>
      <div><strong>{title}</strong><p>{children}</p></div>
    </li>
  )
}

function Callout({
  icon,
  title,
  children,
}: {
  icon: ReactNode
  title: string
  children: ReactNode
}) {
  return (
    <aside className="operator-workflow-callout">
      {icon}
      <div><strong>{title}</strong><p>{children}</p></div>
    </aside>
  )
}
