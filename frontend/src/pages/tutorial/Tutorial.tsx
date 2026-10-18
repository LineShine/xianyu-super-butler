import { Link } from 'react-router-dom'
export function Tutorial() {return <div className="card p-8 space-y-6"><h1 className="text-2xl font-extrabold">使用指南</h1><ol className="list-decimal pl-6 space-y-4"><li><Link to="/accounts">添加账号</Link>，登录后确认在线状态。</li><li><Link to="/items">同步商品</Link>，在<CardLink/>中准备发货内容并关联商品。</li><li>在账号和卡券设置中按需开启自动发货、补发货及回复功能。</li><li>在订单和消息日志中检查发货结果与失败原因。</li></ol><p>双击“启动鱼太咸”启动，双击“停止鱼太咸”正常停止。关闭浏览器不会停止后台服务。</p></div>}
function CardLink(){return <Link to="/cards">卡券管理</Link>}
