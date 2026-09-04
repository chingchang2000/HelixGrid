package dev.helixgrid.verifier;

import java.io.IOException;
import java.io.Reader;
import java.io.StringReader;
import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;

/**
 * Tiny, strict JSON parser/serializer used by the replay verifier.
 *
 * <p>The verifier deliberately has zero third-party runtime dependencies so it can be
 * copied into incident-response environments and run with only a JDK. This class is not
 * intended to compete with Jackson/Gson; it implements exactly the RFC 8259 data model
 * needed by HelixGrid event JSONL streams.</p>
 */
public final class Json {
    private Json() {}

    public sealed interface Value permits Obj, Arr, Str, Num, Bool, Null {
        default Obj asObject() {
            if (this instanceof Obj value) return value;
            throw new JsonException("expected object but got " + typeName());
        }

        default Arr asArray() {
            if (this instanceof Arr value) return value;
            throw new JsonException("expected array but got " + typeName());
        }

        default String asString() {
            if (this instanceof Str value) return value.value();
            throw new JsonException("expected string but got " + typeName());
        }

        default BigDecimal asNumber() {
            if (this instanceof Num value) return value.value();
            throw new JsonException("expected number but got " + typeName());
        }

        default boolean asBoolean() {
            if (this instanceof Bool value) return value.value();
            throw new JsonException("expected boolean but got " + typeName());
        }

        default String typeName() {
            return switch (this) {
                case Obj ignored -> "object";
                case Arr ignored -> "array";
                case Str ignored -> "string";
                case Num ignored -> "number";
                case Bool ignored -> "boolean";
                case Null ignored -> "null";
            };
        }
    }

    public record Obj(Map<String, Value> values) implements Value {
        public Obj {
            values = Map.copyOf(values);
        }

        public Value get(String key) {
            return values.get(key);
        }

        public Value require(String key) {
            var value = values.get(key);
            if (value == null) throw new JsonException("missing required property: " + key);
            return value;
        }

        public String string(String key, String fallback) {
            var value = values.get(key);
            if (value == null || value instanceof Null) return fallback;
            return value.asString();
        }

        public long longValue(String key, long fallback) {
            var value = values.get(key);
            if (value == null || value instanceof Null) return fallback;
            try {
                return value.asNumber().longValueExact();
            } catch (ArithmeticException ex) {
                throw new JsonException("property " + key + " is not an integer", ex);
            }
        }

        public boolean booleanValue(String key, boolean fallback) {
            var value = values.get(key);
            if (value == null || value instanceof Null) return fallback;
            return value.asBoolean();
        }

        public Obj object(String key) {
            var value = values.get(key);
            if (value == null || value instanceof Null) return new Obj(Map.of());
            return value.asObject();
        }

        public Arr array(String key) {
            var value = values.get(key);
            if (value == null || value instanceof Null) return new Arr(List.of());
            return value.asArray();
        }
    }

    public record Arr(List<Value> values) implements Value {
        public Arr {
            values = List.copyOf(values);
        }
    }

    public record Str(String value) implements Value {
        public Str {
            Objects.requireNonNull(value, "value");
        }
    }

    public record Num(BigDecimal value) implements Value {
        public Num {
            Objects.requireNonNull(value, "value");
        }
    }

    public record Bool(boolean value) implements Value {}

    public enum Null implements Value {
        INSTANCE
    }

    public static final class JsonException extends RuntimeException {
        private static final long serialVersionUID = 1L;

        public JsonException(String message) {
            super(message);
        }

        public JsonException(String message, Throwable cause) {
            super(message, cause);
        }
    }

    public static Value parse(String source) {
        Objects.requireNonNull(source, "source");
        try {
            return parse(new StringReader(source));
        } catch (IOException impossible) {
            throw new AssertionError(impossible);
        }
    }

    public static Value parse(Reader reader) throws IOException {
        var parser = new Parser(reader);
        var value = parser.parseValue();
        parser.skipWhitespace();
        if (parser.peek() != -1) {
            throw parser.error("trailing data after JSON value");
        }
        return value;
    }

    public static String stringify(Value value) {
        var out = new StringBuilder();
        writeValue(out, value);
        return out.toString();
    }

    public static Value fromJava(Object value) {
        if (value == null) return Null.INSTANCE;
        if (value instanceof Value json) return json;
        if (value instanceof String string) return new Str(string);
        if (value instanceof Boolean bool) return new Bool(bool);
        if (value instanceof Byte || value instanceof Short || value instanceof Integer || value instanceof Long) {
            return new Num(BigDecimal.valueOf(((Number) value).longValue()));
        }
        if (value instanceof Float || value instanceof Double) {
            double number = ((Number) value).doubleValue();
            if (!Double.isFinite(number)) throw new JsonException("JSON cannot encode non-finite number");
            return new Num(BigDecimal.valueOf(number));
        }
        if (value instanceof BigDecimal decimal) return new Num(decimal);
        if (value instanceof Map<?, ?> map) {
            var converted = new LinkedHashMap<String, Value>();
            for (var entry : map.entrySet()) {
                if (!(entry.getKey() instanceof String key)) {
                    throw new JsonException("object keys must be strings");
                }
                converted.put(key, fromJava(entry.getValue()));
            }
            return new Obj(converted);
        }
        if (value instanceof Iterable<?> iterable) {
            var converted = new ArrayList<Value>();
            for (var item : iterable) converted.add(fromJava(item));
            return new Arr(converted);
        }
        throw new JsonException("unsupported Java value: " + value.getClass().getName());
    }

    private static void writeValue(StringBuilder out, Value value) {
        switch (value) {
            case Obj object -> {
                out.append('{');
                boolean first = true;
                for (var entry : object.values().entrySet()) {
                    if (!first) out.append(',');
                    first = false;
                    writeString(out, entry.getKey());
                    out.append(':');
                    writeValue(out, entry.getValue());
                }
                out.append('}');
            }
            case Arr array -> {
                out.append('[');
                for (int i = 0; i < array.values().size(); i++) {
                    if (i > 0) out.append(',');
                    writeValue(out, array.values().get(i));
                }
                out.append(']');
            }
            case Str string -> writeString(out, string.value());
            case Num number -> out.append(number.value().stripTrailingZeros().toPlainString());
            case Bool bool -> out.append(bool.value());
            case Null ignored -> out.append("null");
        }
    }

    private static void writeString(StringBuilder out, String value) {
        out.append('"');
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\b' -> out.append("\\b");
                case '\f' -> out.append("\\f");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                default -> {
                    if (c < 0x20) {
                        out.append("\\u");
                        appendHex4(out, c);
                    } else {
                        out.append(c);
                    }
                }
            }
        }
        out.append('"');
    }

    private static void appendHex4(StringBuilder out, int value) {
        final char[] HEX = "0123456789abcdef".toCharArray();
        out.append(HEX[(value >>> 12) & 0xf]);
        out.append(HEX[(value >>> 8) & 0xf]);
        out.append(HEX[(value >>> 4) & 0xf]);
        out.append(HEX[value & 0xf]);
    }

    private static final class Parser {
        private static final int UNSET = Integer.MIN_VALUE;
        private final Reader reader;
        private int lookahead = UNSET;
        private long offset;
        private int line = 1;
        private int column;

        private Parser(Reader reader) {
            this.reader = Objects.requireNonNull(reader, "reader");
        }

        private Value parseValue() throws IOException {
            skipWhitespace();
            int c = peek();
            return switch (c) {
                case '{' -> parseObject();
                case '[' -> parseArray();
                case '"' -> new Str(parseString());
                case 't' -> {
                    expectKeyword("true");
                    yield new Bool(true);
                }
                case 'f' -> {
                    expectKeyword("false");
                    yield new Bool(false);
                }
                case 'n' -> {
                    expectKeyword("null");
                    yield Null.INSTANCE;
                }
                case '-', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9' -> parseNumber();
                case -1 -> throw error("unexpected end of input");
                default -> throw error("unexpected character " + printable(c));
            };
        }

        private Obj parseObject() throws IOException {
            expect('{');
            skipWhitespace();
            var values = new LinkedHashMap<String, Value>();
            if (consumeIf('}')) return new Obj(values);
            while (true) {
                skipWhitespace();
                if (peek() != '"') throw error("object key must be a string");
                String key = parseString();
                if (values.containsKey(key)) throw error("duplicate object key: " + key);
                skipWhitespace();
                expect(':');
                Value value = parseValue();
                values.put(key, value);
                skipWhitespace();
                if (consumeIf('}')) break;
                expect(',');
            }
            return new Obj(values);
        }

        private Arr parseArray() throws IOException {
            expect('[');
            skipWhitespace();
            var values = new ArrayList<Value>();
            if (consumeIf(']')) return new Arr(values);
            while (true) {
                values.add(parseValue());
                skipWhitespace();
                if (consumeIf(']')) break;
                expect(',');
            }
            return new Arr(values);
        }

        private String parseString() throws IOException {
            expect('"');
            var out = new StringBuilder();
            while (true) {
                int c = read();
                if (c == -1) throw error("unterminated string");
                if (c == '"') return out.toString();
                if (c == '\\') {
                    int escaped = read();
                    switch (escaped) {
                        case '"' -> out.append('"');
                        case '\\' -> out.append('\\');
                        case '/' -> out.append('/');
                        case 'b' -> out.append('\b');
                        case 'f' -> out.append('\f');
                        case 'n' -> out.append('\n');
                        case 'r' -> out.append('\r');
                        case 't' -> out.append('\t');
                        case 'u' -> appendUnicodeEscape(out);
                        case -1 -> throw error("unterminated string escape");
                        default -> throw error("invalid string escape: \\" + (char) escaped);
                    }
                } else {
                    if (c < 0x20) throw error("unescaped control character in string");
                    out.append((char) c);
                }
            }
        }

        private void appendUnicodeEscape(StringBuilder out) throws IOException {
            int first = readHex4();
            char high = (char) first;
            if (Character.isHighSurrogate(high)) {
                if (read() != '\\' || read() != 'u') {
                    throw error("high surrogate must be followed by low surrogate escape");
                }
                int second = readHex4();
                char low = (char) second;
                if (!Character.isLowSurrogate(low)) throw error("invalid low surrogate");
                out.appendCodePoint(Character.toCodePoint(high, low));
            } else if (Character.isLowSurrogate(high)) {
                throw error("unexpected low surrogate");
            } else {
                out.append(high);
            }
        }

        private int readHex4() throws IOException {
            int value = 0;
            for (int i = 0; i < 4; i++) {
                int c = read();
                int digit = Character.digit(c, 16);
                if (digit < 0) throw error("invalid unicode escape");
                value = (value << 4) | digit;
            }
            return value;
        }

        private Num parseNumber() throws IOException {
            var out = new StringBuilder();
            if (consumeIf('-')) out.append('-');
            int c = peek();
            if (c == '0') {
                out.append((char) read());
                if (Character.isDigit(peek())) throw error("leading zero in number");
            } else if (c >= '1' && c <= '9') {
                do {
                    out.append((char) read());
                    c = peek();
                } while (c >= '0' && c <= '9');
            } else {
                throw error("expected digit in number");
            }

            if (consumeIf('.')) {
                out.append('.');
                if (!Character.isDigit(peek())) throw error("fraction requires a digit");
                while (Character.isDigit(peek())) out.append((char) read());
            }

            c = peek();
            if (c == 'e' || c == 'E') {
                out.append((char) read());
                c = peek();
                if (c == '+' || c == '-') out.append((char) read());
                if (!Character.isDigit(peek())) throw error("exponent requires a digit");
                while (Character.isDigit(peek())) out.append((char) read());
            }

            try {
                return new Num(new BigDecimal(out.toString()));
            } catch (NumberFormatException ex) {
                throw error("invalid number: " + out);
            }
        }

        private void expectKeyword(String keyword) throws IOException {
            for (int i = 0; i < keyword.length(); i++) {
                if (read() != keyword.charAt(i)) throw error("expected " + keyword);
            }
        }

        private void skipWhitespace() throws IOException {
            while (true) {
                int c = peek();
                if (c == ' ' || c == '\t' || c == '\r' || c == '\n') read();
                else return;
            }
        }

        private boolean consumeIf(int expected) throws IOException {
            if (peek() != expected) return false;
            read();
            return true;
        }

        private void expect(int expected) throws IOException {
            int actual = read();
            if (actual != expected) {
                throw error("expected " + printable(expected) + " but got " + printable(actual));
            }
        }

        private int peek() throws IOException {
            if (lookahead == UNSET) lookahead = reader.read();
            return lookahead;
        }

        private int read() throws IOException {
            int c;
            if (lookahead != UNSET) {
                c = lookahead;
                lookahead = UNSET;
            } else {
                c = reader.read();
            }
            if (c != -1) {
                offset++;
                if (c == '\n') {
                    line++;
                    column = 0;
                } else {
                    column++;
                }
            }
            return c;
        }

        private JsonException error(String message) {
            return new JsonException(message + " at line " + line + ", column " + column + " (offset " + offset + ")");
        }

        private static String printable(int c) {
            if (c == -1) return "end of input";
            if (c < 0x20 || c == 0x7f) return String.format("U+%04X", c);
            return "'" + (char) c + "'";
        }
    }
}
