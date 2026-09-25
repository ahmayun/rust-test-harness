//! Minimal smoke tests for the external-tester harness.
//! Uses only stable std APIs so any recent rustc sysroot can run them.

#[test]
fn seq_compare() {
    assert!("hello".to_string() < "hellr".to_string());
    assert!("hello ".to_string() > "hello".to_string());
    assert!("hello".to_string() != "there".to_string());
    assert!(vec![1, 2, 3, 4] > vec![1, 2, 3]);
    assert!(vec![1, 2, 3] < vec![1, 2, 3, 4]);
    assert_eq!(vec![1, 2, 3], vec![1, 2, 3]);
}

#[test]
fn string_basics() {
    let s = String::from("external-tester");
    assert_eq!(s.len(), 15);
    assert!(s.contains("tester"));
}

#[test]
fn vec_push_pop() {
    let mut v = vec![1, 2];
    v.push(3);
    assert_eq!(v.pop(), Some(3));
    assert_eq!(v, [1, 2]);
}
