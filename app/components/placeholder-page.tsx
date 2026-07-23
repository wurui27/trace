import Link from "next/link";

interface PlaceholderPageProps {
  readonly title: string;
}

export function PlaceholderPage({ title }: PlaceholderPageProps) {
  return (
    <section className="placeholder-page" aria-labelledby="placeholder-title">
      <p className="page-eyebrow">PerfPilot</p>
      <h1 id="placeholder-title">{title}</h1>
      <p className="placeholder-description">
        该能力尚未在当前前端切片中接入。
      </p>
      <Link className="placeholder-back-link" href="/">
        返回性能总览
      </Link>
    </section>
  );
}
