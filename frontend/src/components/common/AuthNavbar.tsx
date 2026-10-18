import { Link } from 'react-router-dom'
export function AuthNavbar({ systemName = '鱼太咸' }: { systemName?: string }) {
  return <nav className="absolute top-6 left-8 z-20"><Link className="font-bold text-gray-700" to="/login">{systemName}</Link></nav>
}
export function PublicPageFooter({ systemName = '鱼太咸' }: { systemName?: string }) {
  return <footer className="py-8 text-center text-xs text-gray-400">{systemName} · 闲鱼卖家自动托管助手</footer>
}
