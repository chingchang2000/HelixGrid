using System.Drawing.Drawing2D;

namespace HelixGrid.Dashboard;

internal static class Theme
{
    public static readonly Color Background = Color.FromArgb(8, 12, 22);
    public static readonly Color Sidebar = Color.FromArgb(11, 17, 30);
    public static readonly Color Surface = Color.FromArgb(16, 24, 40);
    public static readonly Color Surface2 = Color.FromArgb(21, 31, 51);
    public static readonly Color Surface3 = Color.FromArgb(26, 39, 63);
    public static readonly Color Border = Color.FromArgb(39, 53, 78);
    public static readonly Color Text = Color.FromArgb(242, 246, 252);
    public static readonly Color Muted = Color.FromArgb(145, 160, 186);
    public static readonly Color Accent = Color.FromArgb(92, 161, 255);
    public static readonly Color AccentHover = Color.FromArgb(120, 178, 255);
    public static readonly Color Green = Color.FromArgb(80, 211, 138);
    public static readonly Color Yellow = Color.FromArgb(247, 198, 84);
    public static readonly Color Red = Color.FromArgb(255, 104, 124);

    public static Font Font(float size = 10f, FontStyle style = FontStyle.Regular) =>
        new("Segoe UI", size, style, GraphicsUnit.Point);
}

internal sealed class RoundedPanel : Panel
{
    public int Radius { get; set; } = 18;
    public Color BorderColor { get; set; } = Theme.Border;
    public int BorderWidth { get; set; } = 1;

    public RoundedPanel()
    {
        DoubleBuffered = true;
        BackColor = Theme.Surface;
        Padding = new Padding(18);
        Margin = new Padding(0);
    }

    protected override void OnResize(EventArgs eventargs)
    {
        base.OnResize(eventargs);
        Region?.Dispose();
        Region = new Region(RoundedRect(ClientRectangle, Radius));
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        var rect = ClientRectangle;
        rect.Width -= 1;
        rect.Height -= 1;
        using var path = RoundedRect(rect, Radius);
        using var pen = new Pen(BorderColor, BorderWidth);
        e.Graphics.DrawPath(pen, path);
    }

    public static GraphicsPath RoundedRect(Rectangle rect, int radius)
    {
        var diameter = Math.Max(2, radius * 2);
        var arc = new Rectangle(rect.X, rect.Y, diameter, diameter);
        var path = new GraphicsPath();
        path.AddArc(arc, 180, 90);
        arc.X = rect.Right - diameter;
        path.AddArc(arc, 270, 90);
        arc.Y = rect.Bottom - diameter;
        path.AddArc(arc, 0, 90);
        arc.X = rect.Left;
        path.AddArc(arc, 90, 90);
        path.CloseFigure();
        return path;
    }
}

internal class ModernButton : Button
{
    private Color _normal = Theme.Surface3;
    private Color _hover = Color.FromArgb(36, 53, 82);

    public int Radius { get; set; } = 10;

    public Color NormalColor
    {
        get => _normal;
        set { _normal = value; BackColor = value; Invalidate(); }
    }

    public Color HoverColor
    {
        get => _hover;
        set => _hover = value;
    }

    public ModernButton()
    {
        FlatStyle = FlatStyle.Flat;
        FlatAppearance.BorderSize = 0;
        BackColor = _normal;
        ForeColor = Theme.Text;
        Font = Theme.Font(9.5f, FontStyle.Bold);
        Cursor = Cursors.Hand;
        Height = 40;
        Padding = new Padding(12, 0, 12, 0);
        TextAlign = ContentAlignment.MiddleCenter;

        MouseEnter += (_, _) => BackColor = _hover;
        MouseLeave += (_, _) => BackColor = _normal;
        Resize += (_, _) => UpdateRegion();
    }

    protected override void OnCreateControl()
    {
        base.OnCreateControl();
        UpdateRegion();
    }

    private void UpdateRegion()
    {
        if (Width <= 0 || Height <= 0)
            return;
        Region?.Dispose();
        Region = new Region(RoundedPanel.RoundedRect(ClientRectangle, Radius));
    }
}

internal sealed class AccentButton : ModernButton
{
    public AccentButton()
    {
        NormalColor = Theme.Accent;
        HoverColor = Theme.AccentHover;
        ForeColor = Color.FromArgb(6, 17, 31);
    }
}

internal sealed class DangerButton : ModernButton
{
    public DangerButton()
    {
        NormalColor = Color.FromArgb(83, 31, 43);
        HoverColor = Color.FromArgb(112, 39, 55);
        ForeColor = Color.FromArgb(255, 199, 207);
    }
}

internal sealed class NavButton : Button
{
    private bool _selected;

    public bool Selected
    {
        get => _selected;
        set { _selected = value; Invalidate(); }
    }

    public NavButton()
    {
        FlatStyle = FlatStyle.Flat;
        FlatAppearance.BorderSize = 0;
        BackColor = Color.Transparent;
        ForeColor = Theme.Muted;
        Font = Theme.Font(10f, FontStyle.Bold);
        Height = 48;
        TextAlign = ContentAlignment.MiddleLeft;
        Padding = new Padding(22, 0, 0, 0);
        Cursor = Cursors.Hand;
        Dock = DockStyle.Top;
    }

    protected override void OnPaint(PaintEventArgs pevent)
    {
        pevent.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        pevent.Graphics.Clear(_selected ? Color.FromArgb(24, 38, 61) : Parent?.BackColor ?? Theme.Sidebar);

        if (_selected)
        {
            using var brush = new SolidBrush(Theme.Accent);
            pevent.Graphics.FillRectangle(brush, 0, 8, 4, Height - 16);
        }

        TextRenderer.DrawText(
            pevent.Graphics,
            Text,
            Font,
            new Rectangle(Padding.Left, 0, Width - Padding.Left, Height),
            _selected ? Theme.Text : Theme.Muted,
            TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis);
    }

    protected override void OnMouseEnter(EventArgs e)
    {
        base.OnMouseEnter(e);
        if (!_selected) ForeColor = Theme.Text;
        Invalidate();
    }

    protected override void OnMouseLeave(EventArgs e)
    {
        base.OnMouseLeave(e);
        if (!_selected) ForeColor = Theme.Muted;
        Invalidate();
    }
}
