// StoreHelper.exe — Microsoft Store purchase shim for Chairside Ready Alert.
//
// The parent app is a Python/Tkinter program packaged as MSIX. Calling
// Windows.Services.Store.StoreContext.RequestPurchaseAsync from a Win32
// desktop host requires the SDK to know which window to anchor the purchase
// overlay to, via IInitializeWithWindow.Initialize(hwnd). That interface is
// classic COM, not WinRT, so the Python winrt projection cannot reach it —
// the call fails with "must be called from a UI thread" (RPC_E_WRONG_THREAD).
//
// This binary does the IInitializeWithWindow handshake natively, then runs
// the Store call. Python invokes it via subprocess, parses stdout, and uses
// the exit code as the success/fail signal.
//
// Usage: StoreHelper.exe <product_id> [hwnd_decimal]
//   Exit 0  — purchase succeeded or already owned by the user.
//   Exit 1  — Store returned a non-success status (cancelled, network, etc.).
//   Exit 2  — bad command-line arguments.
//   Exit 3  — exception thrown inside the helper.
// Stdout : "STATUS=<StorePurchaseStatus>" and optionally "EXTENDED_ERROR=<text>".
// Stderr : usage message or "EXCEPTION: <Type>: <message>".

using System;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using Windows.Services.Store;

namespace ChairsideReadyAlert.StoreHelper;

internal static class Program
{
    [ComImport]
    [Guid("3E68D4BD-7135-4D10-8018-9FB6D9F33FA1")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IInitializeWithWindow
    {
        void Initialize(IntPtr hwnd);
    }

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    private static async Task<int> Main(string[] args)
    {
        if (args.Length < 1 || args.Length > 2)
        {
            Console.Error.WriteLine("usage: StoreHelper.exe <product_id> [hwnd_decimal]");
            return 2;
        }

        string productId = args[0];

        IntPtr hwnd;
        if (args.Length >= 2 && long.TryParse(args[1], out long parsedHwnd) && parsedHwnd != 0)
        {
            hwnd = new IntPtr(parsedHwnd);
        }
        else
        {
            hwnd = GetForegroundWindow();
        }

        try
        {
            StoreContext ctx = StoreContext.GetDefault();
            IInitializeWithWindow init = (IInitializeWithWindow)(object)ctx;
            init.Initialize(hwnd);

            StorePurchaseResult result = await ctx.RequestPurchaseAsync(productId);
            Console.WriteLine($"STATUS={result.Status}");
            if (result.ExtendedError is not null)
            {
                string msg = result.ExtendedError.Message.Replace("\r", " ").Replace("\n", " ");
                Console.WriteLine($"EXTENDED_ERROR={msg}");
            }

            return result.Status switch
            {
                StorePurchaseStatus.Succeeded => 0,
                StorePurchaseStatus.AlreadyPurchased => 0,
                _ => 1,
            };
        }
        catch (Exception ex)
        {
            string msg = ex.Message.Replace("\r", " ").Replace("\n", " ");
            Console.Error.WriteLine($"EXCEPTION: {ex.GetType().Name}: {msg}");
            return 3;
        }
    }
}
