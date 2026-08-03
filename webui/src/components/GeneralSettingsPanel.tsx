import { Image, Map, RotateCcw, ScanLine, Settings, View, X } from 'lucide-react'
import type { ReactNode } from 'react'
import type { UserSettings, UserSettingsPatch } from '../lib/userSettings'
import './GeneralSettingsPanel.css'

export function GeneralSettingsPanel({
  settings,
  externalAction,
  onChange,
  onReset,
  onClose,
}: {
  settings: UserSettings
  externalAction?: ReactNode
  onChange: (patch: UserSettingsPatch) => void
  onReset: () => void
  onClose?: () => void
}) {
  return (
    <aside className="general-settings-panel" aria-label="일반 설정">
      <header className="general-settings-heading">
        <div>
          <span className="general-settings-icon" aria-hidden="true">
            <Settings size={18} />
          </span>
          <span>
            <small>GENERAL SETTINGS</small>
            <h2>일반 설정</h2>
          </span>
        </div>
        <div className="general-settings-actions">
          {externalAction}
          {onClose && (
            <button
              type="button"
              className="icon-button"
              aria-label="일반 설정 닫기"
              title="일반 설정 닫기"
              onClick={onClose}
            >
              <X size={16} />
            </button>
          )}
        </div>
      </header>

      <div className="general-settings-scroll">
        <section className="general-settings-section" aria-labelledby="panorama-settings-title">
          <div className="general-settings-section-title">
            <View size={17} aria-hidden="true" />
            <span>
              <h3 id="panorama-settings-title">파노라마</h3>
              <small>프레임을 이동할 때 적용할 기본 보기 설정입니다.</small>
            </span>
          </div>

          <label className="general-settings-field">
            <span>
              <strong>정면 보정각</strong>
              <small>양수는 오른쪽, 음수는 왼쪽으로 정면을 보정합니다.</small>
            </span>
            <span className="general-settings-number">
              <input
                type="number"
                aria-label="파노라마 정면 보정각"
                min={-180}
                max={180}
                step={0.5}
                value={settings.panoramaForwardOffsetDeg}
                onChange={(event) =>
                  onChange({ panoramaForwardOffsetDeg: Number(event.target.value) })
                }
              />
              <em>°</em>
            </span>
          </label>

          <label className="general-settings-field">
            <span>
              <strong>기본 화질</strong>
              <small>새 파노라마를 열 때 사용할 이미지 크기입니다.</small>
            </span>
            <select
              aria-label="파노라마 기본 화질"
              value={settings.panoramaDefaultQuality}
              onChange={(event) =>
                onChange({
                  panoramaDefaultQuality: event.target.value as UserSettings['panoramaDefaultQuality'],
                })
              }
            >
              <option value="fast">빠름 · 최대 2K</option>
              <option value="high">고화질 · 4K</option>
              <option value="ultra">최고화질 · 8K</option>
            </select>
          </label>
        </section>

        <section className="general-settings-section" aria-labelledby="overlay-settings-title">
          <div className="general-settings-section-title">
            <ScanLine size={17} aria-hidden="true" />
            <span>
              <h3 id="overlay-settings-title">파노라마 포인트</h3>
              <small>파노라마 위에 같은 프레임의 3D 포인트를 겹쳐 표시합니다.</small>
            </span>
          </div>

          <label className="general-settings-switch-row">
            <span>
              <strong>포인트 오버레이</strong>
              <small>{settings.panoramaPointOverlayEnabled ? '표시 중' : '숨김'}</small>
            </span>
            <input
              type="checkbox"
              aria-label="파노라마 포인트 오버레이 표시"
              checked={settings.panoramaPointOverlayEnabled}
              onChange={(event) =>
                onChange({ panoramaPointOverlayEnabled: event.target.checked })
              }
            />
          </label>

          <label className="general-settings-slider">
            <span>
              <strong>포인트 투명도</strong>
              <output>{Math.round(settings.panoramaPointOverlayOpacity * 100)}%</output>
            </span>
            <input
              type="range"
              aria-label="파노라마 포인트 투명도"
              min={0}
              max={1}
              step={0.05}
              value={settings.panoramaPointOverlayOpacity}
              disabled={!settings.panoramaPointOverlayEnabled}
              onChange={(event) =>
                onChange({ panoramaPointOverlayOpacity: Number(event.target.value) })
              }
            />
          </label>
        </section>

        <section className="general-settings-section" aria-labelledby="map-settings-title">
          <div className="general-settings-section-title">
            <Map size={17} aria-hidden="true" />
            <span>
              <h3 id="map-settings-title">지도</h3>
              <small>복잡한 경로의 기본 표시 범위를 정합니다.</small>
            </span>
          </div>

          <label className="general-settings-switch-row">
            <span>
              <strong>모든 트랙 함께 표시</strong>
              <small>
                {settings.showAllMapTracks
                  ? '전체 트랙을 각 색상으로 표시합니다.'
                  : '활성 트랙만 색상으로 표시합니다.'}
              </small>
            </span>
            <input
              type="checkbox"
              aria-label="지도에 모든 트랙 표시"
              checked={settings.showAllMapTracks}
              onChange={(event) => onChange({ showAllMapTracks: event.target.checked })}
            />
          </label>
        </section>

        <div className="general-settings-note" role="note">
          <Image size={15} aria-hidden="true" />
          이 설정은 현재 브라우저에 저장되며 다음 접속과 분리 창에서도 유지됩니다.
        </div>
      </div>

      <footer className="general-settings-footer">
        <button type="button" className="general-settings-reset" onClick={onReset}>
          <RotateCcw size={15} aria-hidden="true" />
          기본 설정으로 복원
        </button>
      </footer>
    </aside>
  )
}
