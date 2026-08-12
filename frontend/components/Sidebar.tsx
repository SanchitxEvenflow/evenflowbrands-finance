"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";

export default function Sidebar() {
  const pathname = usePathname();

  const links = [
    { name: "Email Credit Notes", href: "/credit-notes", icon: "📧" },
    {
      name: "Create Credit Note",
      href: "/create-credit-note",
      icon: "➕",
      subLinks: [
        { name: "Blinkit", href: "/create-credit-note/blinkit" },
        { name: "Swiggy", href: "/create-credit-note/swiggy" },
      ]
    },
    { 
      name: "GRN Push", 
      href: "/grn-push", 
      icon: "📦",
      subLinks: [
        { name: "Bills", href: "/grn-push" },
        { name: "History", href: "/grn-push/history" },
      ]
    },
    { name: "TDS Fetcher", href: "/tds-fetcher", icon: "🧾" },
    { name: "Extract VRET PDF", href: "/extract-vret-pdf", icon: "📄" },
  ];

  return (
    <div className="w-64 bg-black min-h-screen flex flex-col fixed left-0 top-0 text-slate-300 shadow-xl z-50">
      <div className="p-6 pt-8 pb-6 flex items-center justify-center border-b border-white/10">
        <Image src="/logo.png" className="h-20 w-auto object-contain" alt="Evenflow logo" width={240} height={96} priority />
      </div>
      <div className="flex-1 py-8 px-4 flex flex-col gap-2">
        <div className="px-4 text-xs font-semibold text-slate-500 uppercase tracking-wider mb-2">Tools</div>
        {links.map((link) => {
          const isParentActive = pathname.startsWith(link.href);
          const hasSubLinks = link.subLinks && link.subLinks.length > 0;

          return (
            <div key={link.name} className="flex flex-col gap-1">
              <Link
                href={link.href}
                className={`flex items-center gap-3 px-4 py-3 rounded-xl transition-all ${
                  isParentActive && !hasSubLinks
                    ? "bg-indigo-500/10 text-indigo-400 font-medium"
                    : isParentActive
                    ? "text-indigo-400 font-medium"
                    : "hover:bg-white/5 hover:text-white font-medium"
                }`}
              >
                <span className="text-xl">{link.icon}</span>
                <span>{link.name}</span>
              </Link>
              {isParentActive && hasSubLinks && (
                <div className="flex flex-col gap-1 ml-11 mt-1 border-l border-white/10 pl-3">
                  {link.subLinks?.map((subLink) => {
                    const isSubActive = pathname === subLink.href;
                    return (
                      <Link
                        key={subLink.name}
                        href={subLink.href}
                        className={`px-3 py-2 text-sm rounded-lg transition-all ${
                          isSubActive
                            ? "bg-indigo-500/20 text-indigo-300 font-medium"
                            : "text-slate-400 hover:bg-white/5 hover:text-white"
                        }`}
                      >
                        {subLink.name}
                      </Link>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
