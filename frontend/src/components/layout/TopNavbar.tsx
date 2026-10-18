import { LogOut } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { useAuthStore } from '@/store/authStore'
import { logout } from '@/api/auth'
export function TopNavbar({systemName = '鱼太咸'}: {systemName?: string}) {
 const {user, clearAuth} = useAuthStore(); const navigate=useNavigate()
 return <header className="h-16 px-8 flex justify-between items-center border-b border-gray-100 bg-white/80 backdrop-blur-xl"><span className="text-sm font-medium text-gray-500">{systemName} · 卖家工作台</span><div className="flex items-center gap-5 text-sm"><span>{user?.username}</span><button className="flex items-center gap-2 text-gray-500 hover:text-red-500" onClick={async()=>{try{await logout()}finally{clearAuth();navigate('/login')}}}><LogOut size={16}/>退出登录</button></div></header>
}
