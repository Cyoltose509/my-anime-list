import { Date, getDate } from "./Date"
import { QuartzComponentConstructor, QuartzComponentProps } from "./types"
import readingTime from "reading-time"
import { classNames } from "../util/lang"
import { i18n } from "../i18n"
import { JSX } from "preact"
import style from "./styles/contentMeta.scss"

interface ContentMetaOptions {
  showReadingTime: boolean
  showComma: boolean
  // 新增：可选配置要显示哪些自定义 frontmatter 字段（未来扩展用）
  showCustomFields?: string[]  // e.g. ["source", "type"]
}

const defaultOptions: ContentMetaOptions = {
  showReadingTime: true,
  showComma: true,
  showCustomFields: ["source"],  // 默认显示 source，你可以改成 [] 或加更多
}

export default ((opts?: Partial<ContentMetaOptions>) => {
  const options: ContentMetaOptions = { ...defaultOptions, ...opts }

  function ContentMetadata({ cfg, fileData, displayClass }: QuartzComponentProps) {
    const text = fileData.text

    if (!text) {
      return null
    }

    const segments: (string | JSX.Element)[] = []

    // 原有：日期
    if (fileData.dates) {
      segments.push(<Date date={getDate(cfg, fileData)!} locale={cfg.locale} />)
    }

    // 原有：阅读时间
    if (options.showReadingTime) {
      const { minutes } = readingTime(text)
      const displayedTime = i18n(cfg.locale).components.contentMeta.readingTime({
        minutes: Math.ceil(minutes),
      })
      segments.push(<span>{displayedTime}</span>)
    }

    // 新增：显示自定义 frontmatter 字段（这里以 source 为例）
    const frontmatter = fileData.frontmatter as Record<string, unknown> ?? {}
    if (options.showCustomFields && options.showCustomFields.length > 0) {
      for (const field of options.showCustomFields) {
        const value = frontmatter[field]
        if (value) {
          let display: JSX.Element | string

          // 根据字段类型特殊处理（比如 source 是 URL，就做成链接）
          if (field === "source" && typeof value === "string" && value.startsWith("http")) {
            display = (
              <a
                href={value}
                target="_blank"
                rel="noopener noreferrer"
                class="source-link"  // 可以加 CSS 美化
              >
                百科链接
              </a>
            )
            segments.push(
              <span>
              {display}
            </span>
            )
          } else {
            // 其他字段直接显示值（字符串/数组简单 toString）
            display = typeof value === "string" ? value : JSON.stringify(value)
            segments.push(
              <span>
              {field}: {display}
            </span>
            )
          }


        }
      }
    }

    // 如果有内容，渲染
    if (segments.length > 0) {
      return (
        <p
          show-comma={options.showComma}
          class={classNames(displayClass, "content-meta")}
        >
          {segments.map((seg, idx) => (
            <>
              {seg}
              {idx < segments.length - 1 && options.showComma && ", "}
            </>
          ))}
        </p>
      )
    }

    return null
  }

  ContentMetadata.css = style

  return ContentMetadata
}) satisfies QuartzComponentConstructor