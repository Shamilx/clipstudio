import type { ReactNode } from 'react'

interface IconProps {
  size?: number
  className?: string
}

function make(children: ReactNode) {
  return function Icon({ size = 16, className }: IconProps) {
    return (
      <svg
        width={size}
        height={size}
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
        className={className}
      >
        {children}
      </svg>
    )
  }
}

export const PlusIcon = make(<path d="M8 3v10M3 8h10" />)
export const LinkIcon = make(
  <>
    <path d="M6.5 9.5a3 3 0 0 0 4.2 0l2-2a3 3 0 0 0-4.2-4.2l-.7.7" />
    <path d="M9.5 6.5a3 3 0 0 0-4.2 0l-2 2a3 3 0 0 0 4.2 4.2l.7-.7" />
  </>,
)
export const UploadIcon = make(
  <>
    <path d="M8 11V3M5 6l3-3 3 3" />
    <path d="M3 11v2h10v-2" />
  </>,
)
export const PlayIcon = make(<path d="M5 3.5v9l7-4.5-7-4.5Z" fill="currentColor" />)
export const PauseIcon = make(
  <>
    <path d="M5.5 3.5v9M10.5 3.5v9" strokeWidth="2" />
  </>,
)
export const CheckIcon = make(<path d="m3 8.5 3.2 3.2L13 4.8" />)
export const XIcon = make(<path d="m4 4 8 8M12 4l-8 8" />)
export const DownloadIcon = make(
  <>
    <path d="M8 3v8M5 8l3 3 3-3" />
    <path d="M3 13h10" />
  </>,
)
export const CopyIcon = make(
  <>
    <rect x="5.5" y="5.5" width="7.5" height="7.5" rx="1.5" />
    <path d="M10.5 5.5v-1a1.5 1.5 0 0 0-1.5-1.5H4.5A1.5 1.5 0 0 0 3 4.5V9a1.5 1.5 0 0 0 1.5 1.5h1" />
  </>,
)
export const RefreshIcon = make(
  <>
    <path d="M13 8a5 5 0 1 1-1.5-3.5" />
    <path d="M13 2.5v3h-3" />
  </>,
)
export const TrashIcon = make(
  <>
    <path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.5 8.5h6l.5-8.5" />
  </>,
)
export const ChevronLeftIcon = make(<path d="m10 3.5-4.5 4.5 4.5 4.5" />)
export const ChevronRightIcon = make(<path d="m6 3.5 4.5 4.5L6 12.5" />)
export const ChevronDownIcon = make(<path d="m3.5 6 4.5 4.5L12.5 6" />)
export const SettingsIcon = make(
  <>
    <circle cx="8" cy="8" r="2" />
    <path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4" />
  </>,
)
export const ListIcon = make(<path d="M5.5 4h8M5.5 8h8M5.5 12h8M2.5 4h.01M2.5 8h.01M2.5 12h.01" />)
export const LayersIcon = make(
  <>
    <path d="m8 2 6 3-6 3-6-3 6-3Z" />
    <path d="m2 8 6 3 6-3M2 11l6 3 6-3" />
  </>,
)
export const RadioIcon = make(
  <>
    <circle cx="8" cy="8" r="1.5" />
    <path d="M5 5a4.2 4.2 0 0 0 0 6M11 5a4.2 4.2 0 0 1 0 6M3 3a7 7 0 0 0 0 10M13 3a7 7 0 0 1 0 10" />
  </>,
)
export const FilmIcon = make(
  <>
    <rect x="2.5" y="2.5" width="11" height="11" rx="1.5" />
    <path d="M5.5 2.5v11M10.5 2.5v11M2.5 6h3M2.5 10h3M10.5 6h3M10.5 10h3" />
  </>,
)
export const ArrowUpIcon = make(<path d="M8 13V3M4 7l4-4 4 4" />)
export const ArrowDownIcon = make(<path d="M8 3v10M4 9l4 4 4-4" />)
export const VolumeIcon = make(
  <>
    <path d="M7 3.5 4.5 6H2.5v4h2L7 12.5v-9Z" fill="currentColor" />
    <path d="M10 6a3 3 0 0 1 0 4" />
  </>,
)
export const MuteIcon = make(
  <>
    <path d="M7 3.5 4.5 6H2.5v4h2L7 12.5v-9Z" fill="currentColor" />
    <path d="m10 6 4 4M14 6l-4 4" />
  </>,
)
