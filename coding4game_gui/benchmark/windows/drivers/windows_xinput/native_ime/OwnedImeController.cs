using System;
using System.Diagnostics;
using System.IO;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;
using System.Web.Script.Serialization;

class OwnedImeController {
    [DllImport("kernel32",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr LoadLibrary(string path);
    [DllImport("kernel32",CharSet=CharSet.Ansi,SetLastError=true)] static extern IntPtr GetProcAddress(IntPtr module,string name);
    [DllImport("kernel32")] static extern bool FreeLibrary(IntPtr module);
    [DllImport("kernel32")] static extern void SetLastError(uint error);
    [DllImport("user32",SetLastError=true)] static extern IntPtr SetWindowsHookEx(int kind,IntPtr callback,IntPtr module,uint tid);
    [DllImport("user32",SetLastError=true)] static extern bool UnhookWindowsHookEx(IntPtr hook);
    [DllImport("user32")] static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid);
    [DllImport("user32",CharSet=CharSet.Unicode)] static extern int GetClassName(IntPtr hwnd,StringBuilder value,int count);
    [DllImport("user32",CharSet=CharSet.Unicode)] static extern uint RegisterWindowMessage(string name);
    [DllImport("user32",CharSet=CharSet.Unicode)] static extern IntPtr GetProp(IntPtr hwnd,string name);
    [DllImport("user32",SetLastError=true)] static extern IntPtr SendMessageTimeout(IntPtr hwnd,uint message,UIntPtr w,IntPtr l,uint flags,uint timeout,out UIntPtr result);
    [DllImport("user32")] static extern IntPtr GetKeyboardLayout(uint tid);
    static int Main(string[] args) {
        if(args.Length!=8 || IntPtr.Size!=4) return 2;
        IntPtr module=IntPtr.Zero, hook=IntPtr.Zero;
        var record=new System.Collections.Generic.Dictionary<string,object>();
        var clock=Stopwatch.StartNew(); int exit=1;
        try {
            IntPtr hwnd=new IntPtr(unchecked((int)uint.Parse(args[0])));
            uint expectedPid=uint.Parse(args[1]), pid;
            uint tid=GetWindowThreadProcessId(hwnd,out pid);
            var cls=new StringBuilder(128); GetClassName(hwnd,cls,128);
            if(tid==0 || pid!=expectedPid || cls.ToString()!=args[2]) throw new Exception("Target ownership or class differs");
            Process target=Process.GetProcessById((int)pid);
            double created=(target.StartTime.ToUniversalTime()-new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc)).TotalSeconds;
            if(Math.Abs(created-double.Parse(args[6],CultureInfo.InvariantCulture))>0.001 ||
               !String.Equals(target.ProcessName,Path.GetFileNameWithoutExtension(args[7]),StringComparison.OrdinalIgnoreCase))
                throw new Exception("Target process generation or executable differs");
            int operation=int.Parse(args[3]); if(operation!=1 && operation!=2) throw new Exception("Unknown operation");
            string dll=Path.GetFullPath(args[4]); if(dll!=args[4] || !File.Exists(dll)) throw new Exception("DLL must be an existing absolute path");
            IntPtr hkl=GetKeyboardLayout(tid);
            record["pid"]=pid; record["tid"]=tid; record["hwnd"]=unchecked((uint)hwnd.ToInt32());
            record["operation"]=operation; record["process_created_unix"]=created;
            module=LoadLibrary(dll); if(module==IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
            IntPtr callback=GetProcAddress(module,"OwnedImeHook");
            if(callback==IntPtr.Zero) callback=GetProcAddress(module,"_OwnedImeHook@12");
            if(callback==IntPtr.Zero) callback=GetProcAddress(module,"OwnedImeHook@12");
            if(callback==IntPtr.Zero) throw new Exception("Missing native hook export");
            hook=SetWindowsHookEx(4,callback,module,tid); if(hook==IntPtr.Zero) throw new System.ComponentModel.Win32Exception();
            UIntPtr result;
            SetLastError(0);
            bool delivered=SendMessageTimeout(hwnd,RegisterWindowMessage("C4G.OwnedImeContext.Request.v1"),(UIntPtr)(uint)operation,new IntPtr(0x43473449),2,500,out result)!=IntPtr.Zero;
            int error=Marshal.GetLastWin32Error();
            long state=GetProp(hwnd,"C4G.OwnedImeContext.State.v1").ToInt64();
            record["delivered"]=delivered; record["win32_error"]=error; record["state"]=state;
            record["layout_preserved"]=GetKeyboardLayout(tid)==hkl;
            bool matched=delivered && state==operation && GetKeyboardLayout(tid)==hkl;
            record["status"]=matched ? "verified" : "failed"; exit=matched?0:1;
        } catch(Exception e) { record["status"]="failed"; record["error"]=e.ToString(); }
        finally {
            bool unhooked=hook==IntPtr.Zero || UnhookWindowsHookEx(hook);
            record["hook_removed"]=unhooked;
            if(!unhooked) exit=1;
            if(module!=IntPtr.Zero) FreeLibrary(module);
            record["seconds"]=clock.Elapsed.TotalSeconds; record["game_acceptance"]=false;
            File.WriteAllText(args[5],new JavaScriptSerializer().Serialize(record));
        }
        return exit;
    }
}
