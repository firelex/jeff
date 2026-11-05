namespace Jeff.Sdk.Tests;

public class OrderedMapTests
{
    [Fact]
    public void KeysKeepTheOrderTheyWereAddedInEvenWhenTheyLookLikeNumbers()
    {
        var map = new OrderedMap<int>();
        map.Set("b", 1);
        map.Set("10", 2);
        map.Set("2", 3);
        Assert.Equal(new[] { "b", "10", "2" }, map.Keys);
        Assert.Equal(new[] { 1, 2, 3 }, map.Values);
    }

    [Fact]
    public void SettingAKeyAgainReplacesItsValueInPlace()
    {
        var map = new OrderedMap<string>();
        map.Set("a", "x");
        map.Set("b", "y");
        map.Set("a", "z");
        Assert.Equal(new[] { "a", "b" }, map.Keys);
        Assert.Equal("z", map["a"]);
    }

    [Fact]
    public void AddRefusesARepeatedKeyAndAMissingKeyIsNamed()
    {
        var map = new OrderedMap<int>();
        map.Add("a", 1);
        Assert.Throws<ArgumentException>(() => map.Add("a", 2));
        var missing = Assert.Throws<KeyNotFoundException>(() => map["ghost"]);
        Assert.Contains("'ghost'", missing.Message);
    }

    [Fact]
    public void CopyKeepsKeyOrderAndIsIndependent()
    {
        var map = new OrderedMap<string>();
        map.Set("a", "x");
        map.Set("b", "y");
        map.Set("c", "z");
        var copy = map.Copy();
        Assert.Equal(map.Keys, copy.Keys);
        Assert.Equal(map.Values, copy.Values);
        copy.Set("a", "changed");
        Assert.Equal("x", map["a"]);
        Assert.Equal("changed", copy["a"]);
    }
}
